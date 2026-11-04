import copy
import runpy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from astropy.io import fits

from test_storage_policy import M, fixture, runtime_for


class CompressionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project, self.bundle, _ = fixture(Path(self.temp.name), panels=1)
        self.runtime = runtime_for(self.bundle)
        self.runtime.plan['compression'] = 'gzip2'
        self.runtime.reserve = 20 * 2**30
        self.messages = []
        self.runtime.log = self.messages.append
        self.source = self.runtime.root / 'sample'
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.stage = {'label': 'Test registration', 'bytes_needed': 110 * 2**30 + 1024**2,
                      'compressed_output_bytes': 100 * 2**30,
                      'compression_sources': [str(self.source)]}

    def frame(self, index=1, *, noise=False, channels=3, bitpix='float32', compressed=True):
        shape = (channels, 128, 192) if channels > 1 else (128, 192)
        # Camera-like noise stored as float32, rather than arbitrary 32-bit entropy.
        data = (np.random.default_rng(42).integers(0, 65536, size=shape)
                if noise else np.zeros(shape)).astype(bitpix)
        path = self.source.with_name(f'{self.source.name}_{index:05d}.fit.fz')
        if compressed:
            fits.HDUList([fits.PrimaryHDU(), fits.CompImageHDU(
                data, compression_type='GZIP_2', quantize_level=0)]).writeto(path, overwrite=True)
        else:
            fits.writeto(path, data, overwrite=True)
        return path

    def free_space(self, amount):
        return patch.object(M.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(
            1000 * 2**30, 1000 * 2**30 - amount, amount))

    def test_real_lossless_fits_sizes_reduce_guard_but_keep_reserve_and_margins(self):
        path = self.frame(noise=True)
        expected = min(1.0, max(0.5, path.stat().st_size / self.runtime.image_bytes(path) * 1.15))
        factor = self.runtime.stage_compression_factor(self.stage)
        self.assertAlmostEqual(factor, expected)
        self.assertLess(factor, 1)
        estimate = self.stage['bytes_needed'] - int(110 * 2**30 * (1 - factor))
        need = estimate + self.runtime.reserve
        self.assertLess(need, self.stage['bytes_needed'] + self.runtime.reserve)
        with self.free_space(need):
            self.runtime.space_check(self.stage)
        with self.free_space(need - 1):
            with self.assertRaisesRegex(OSError, r'estimated additional free space.*20.0 GiB reserve'):
                self.runtime.space_check(self.stage)

    def test_never_assumes_better_than_two_to_one(self):
        self.frame()
        self.assertEqual(self.runtime.stage_compression_factor(self.stage), 0.5)

    def test_least_compressible_sample_and_last_frame_are_used(self):
        for index in range(1, 31):
            self.frame(index)
        last = self.frame(30, noise=True)
        expected = min(1.0, max(0.5, last.stat().st_size / self.runtime.image_bytes(last) * 1.15))
        self.assertAlmostEqual(self.runtime.stage_compression_factor(self.stage), expected)
        self.assertIn('samples=24', self.messages[-1])

    def test_measurements_refresh_after_source_changes(self):
        self.frame()
        first = self.runtime.stage_compression_factor(self.stage)
        self.frame(noise=True)
        self.assertGreater(self.runtime.stage_compression_factor(self.stage), first)

    def test_missing_corrupt_or_incompatible_samples_use_full_budget(self):
        for options in ({'compressed': False}, {'channels': 1}, {'bitpix': 'uint16'}):
            with self.subTest(options=options):
                self.frame(**options)
                self.assertEqual(self.runtime.stage_compression_factor(self.stage), 1)
        path = self.frame()
        path.write_bytes(b'not FITS')
        self.assertEqual(self.runtime.stage_compression_factor(self.stage), 1)
        path.unlink()
        self.assertEqual(self.runtime.stage_compression_factor(self.stage), 1)
        self.frame()
        self.stage['compression_sources'].append(str(self.runtime.root / 'missing'))
        self.assertEqual(self.runtime.stage_compression_factor(self.stage), 1)

    def test_uncompressed_runs_old_manifests_and_noneligible_stages_are_unchanged(self):
        self.frame()
        self.runtime.plan['compression'] = 'off'
        self.assertEqual(self.runtime.stage_compression_factor(self.stage), 1)
        self.runtime.plan['compression'] = 'gzip2'
        for key in ('compressed_output_bytes', 'compression_sources'):
            stage = dict(self.stage)
            del stage[key]
            self.assertEqual(self.runtime.stage_compression_factor(stage), 1)
        with self.free_space(self.stage['bytes_needed'] + self.runtime.reserve - 1):
            with self.assertRaises(OSError):
                self.runtime.space_check({'label': 'Input copy', 'bytes_needed': self.stage['bytes_needed']})

    def test_copy_fallback_is_not_discounted(self):
        self.frame()
        self.stage['merge_copy_bytes'] = 90 * 2**30
        self.runtime.symlinks_available = False
        # Half of the output estimate, but all alias-copy bytes and reserve.
        need = 55 * 2**30 + 1024**2 + 90 * 2**30 + self.runtime.reserve
        with self.free_space(need - 1):
            with self.assertRaises(OSError):
                self.runtime.space_check(self.stage)
        with self.free_space(need):
            self.runtime.space_check(self.stage)

    def test_geometry_override_is_not_discounted(self):
        self.frame()
        self.stage['mosaic_geometry'] = 'solved'
        self.runtime.mosaic_bytes = lambda prefix: 200 * 2**30
        with self.free_space(220 * 2**30 - 1):
            with self.assertRaises(OSError):
                self.runtime.space_check(self.stage)
        with self.free_space(220 * 2**30):
            self.runtime.space_check(self.stage)

    def test_compiler_declares_only_similar_sequence_outputs_and_all_nights(self):
        project = copy.deepcopy(self.project)
        project.low_disk_compression = 'gzip2'
        builder = M.SirilCommandBuilder(project)
        builder.build()
        stages = builder.storage_bundle.stages
        for stage in stages:
            eligible = stage['label'].endswith(('background extraction', 'night registration'))
            self.assertEqual('compressed_output_bytes' in stage, eligible, stage['label'])
            if eligible:
                self.assertEqual(stage['compressed_output_bytes'], stage['outputs'][0]['estimated_bytes'])
        cross = next(s for s in stages if s['label'].endswith('cross-night registration'))
        nights = [s['outputs'][0]['path'] for s in stages
                  if s['label'].endswith('/ night registration')]
        self.assertEqual(cross['compression_sources'], nights)
        self.assertEqual(len(nights), 2)

    def test_exported_worker_has_same_budget_logic(self):
        worker = runpy.run_path(str(self.bundle.folder / 'worker.py'))
        exported = worker['StorageRuntime'](self.bundle.manifest_path, None)
        exported.plan['compression'] = 'gzip2'
        exported.log = self.messages.append
        self.frame(noise=True)
        self.assertEqual(exported.stage_compression_factor(self.stage),
                         self.runtime.stage_compression_factor(self.stage))


if __name__ == '__main__':
    unittest.main()
