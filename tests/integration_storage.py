"""Opt-in native Siril comparison; never edits source images or the supplied project.

Example (run with Siril's Python environment):
  python tests/integration_storage.py --project project.json --output C:/scratch/new-test \
      --siril "C:/Program Files/Siril/bin/siril-cli.exe" --config config.1.4.ini
The output directory must not already exist. Uses the first two populated panels
and two nights, four lights and eight flats per unit unless overridden.
"""
import argparse
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
from astropy.io import fits


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def physical_bytes(root):
    total = 0
    identities = set()
    for folder, directories, names in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if not Path(folder, d).is_symlink()]
        for name in names:
            path = Path(folder, name)
            try:
                if path.is_symlink():
                    continue
                info = path.stat()
                identity = (info.st_dev, info.st_ino)
                if identity not in identities:
                    total += info.st_size
                    identities.add(identity)
            except FileNotFoundError:
                pass
    return total


def compare_images(left, right):
    with fits.open(left, memmap=False) as lhs, fits.open(right, memmap=False) as rhs:
        a = next(h for h in lhs if h.header.get('NAXIS', 0) >= 2)
        b = next(h for h in rhs if h.header.get('NAXIS', 0) >= 2)
        if a.data.shape != b.data.shape:
            return {'shape_equal': False, 'left': list(a.data.shape), 'right': list(b.data.shape)}
        finite_a, finite_b = np.isfinite(a.data), np.isfinite(b.data)
        mask_equal = np.array_equal(finite_a, finite_b)
        good = finite_a & finite_b
        diff = a.data[good].astype('float64') - b.data[good].astype('float64')
        ignored = {'HISTORY', 'COMMENT', 'DATE', 'DATE-PRO', 'PROGRAM', 'CREATOR', 'FILENAME', 'CHECKSUM', 'DATASUM'}
        header_differences = {key: [str(a.header.get(key)), str(b.header.get(key))]
                              for key in set(a.header) | set(b.header)
                              if key not in ignored and a.header.get(key) != b.header.get(key)}
        return {'shape_equal': True, 'dtype_equal': a.data.dtype == b.data.dtype,
                'finite_mask_equal': mask_equal, 'pixels_equal': bool(mask_equal and np.all(diff == 0)),
                'different_pixels': int(np.count_nonzero(diff)), 'pixels': int(diff.size),
                'max_abs': float(np.max(np.abs(diff))), 'rms': float(np.sqrt(np.mean(diff**2))),
                'header_differences': header_differences}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--siril', type=Path, required=True)
    ap.add_argument('--config', type=Path, required=True)
    ap.add_argument('--baseline-script', type=Path)
    ap.add_argument('--lights', type=int, default=4)
    ap.add_argument('--flats', type=int, default=8)
    ap.add_argument('--compression', choices=('off', 'gzip2'), default='off')
    ap.add_argument('--background', choices=('on', 'off'), default='off')
    ap.add_argument('--repeat-baseline', action='store_true')
    ap.add_argument('--controlled-no-dither', action='store_true',
                    help='Test only: disable background dithering in BOTH paths to isolate storage changes')
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    module = load_module(root / 'osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py', 'storage_native_current')
    baseline = load_module(args.baseline_script, 'storage_native_baseline') if args.baseline_script else module
    args.output = args.output.absolute()
    args.output.mkdir(parents=True, exist_ok=False)
    source = json.loads(args.project.read_text(encoding='utf-8-sig'))
    source['sessions'] = source['sessions'][:2]
    for session in source['sessions']:
        session['panels'] = [p for p in session['panels'] if p.get('lights')][:2]
        for panel in session['panels']:
            panel['lights'] = panel['lights'][:args.lights]
            panel['flats'] = panel.get('flats', [])[:args.flats]
    source.update(compress_intermediates=False, panel_background_extraction=args.background == 'on',
                  storage_policy='keep_all', low_disk_compression=args.compression, storage_reserve_gib=20)
    original_names = sorted({f for s in source['sessions'] for p in s['panels'] for kind in ('lights', 'flats') for f in p.get(kind, [])})
    before = {name: module.StorageRuntime.fingerprint(name) for name in original_names}
    report = {'runs': {}, 'comparisons': {}, 'compression': args.compression, 'background': args.background,
              'controlled_no_dither': args.controlled_no_dither}
    names = ['baseline', 'baseline_repeat', 'managed'] if args.repeat_baseline else ['baseline', 'managed']
    for name in names:
        engine = module if name == 'managed' else baseline
        project = engine.Project.from_dict(copy.deepcopy(source))
        project.working_dir = str(args.output / name)
        project.storage_policy = 'min_disk' if name == 'managed' else 'keep_all'
        work = Path(project.working_dir)
        work.mkdir()
        if name != 'managed':
            for session in project.sessions:
                for panel in session.panels:
                    proc = work / (session.work_subdir or session.name) / panel.panel_id / 'process'
                    proc.mkdir(parents=True)
                    for kind in ('lights', 'flats'):
                        folder = proc.parent / kind
                        folder.mkdir()
                        for source_file in getattr(panel, kind):
                            src = Path(source_file)
                            # Test inputs use symlinks; refuse to silently duplicate a large dataset.
                            (folder / src.name).symlink_to(src)
        builder = engine.SirilCommandBuilder(project)
        script = builder.build()
        if args.controlled_no_dither:
            script = '\n'.join(line + ' -nodither' if line.startswith('seqsubsky ') else line
                               for line in script.splitlines()) + '\n'
            if name == 'managed':
                for stage in builder.storage_bundle.stages:
                    stage['commands'] = [line + ' -nodither' if line.startswith('seqsubsky ') else line
                                         for line in stage['commands']]
        if name == 'managed':
            builder.storage_bundle.write()
        (work / 'project.json').write_text(json.dumps(project.to_dict(), indent=2), encoding='utf-8')
        script_path = work / 'run_project.ssf'
        script_path.write_text(script, encoding='utf-8')
        config = work / 'siril.ini'
        shutil.copyfile(args.config, config)
        print(f'RUN {name}: {script_path}', flush=True)
        peak = [0]
        stop = threading.Event()
        def sample():
            while not stop.is_set():
                peak[0] = max(peak[0], physical_bytes(work))
                stop.wait(.1)
        thread = threading.Thread(target=sample, daemon=True)
        thread.start()
        started = time.time()
        try:
            with (work / 'cli.log').open('wb') as log:
                proc = subprocess.run([str(args.siril), '-i', str(config), '-s', str(script_path)],
                                      cwd=work, stdout=log, stderr=subprocess.STDOUT)
        finally:
            stop.set()
            thread.join()
        peak[0] = max(peak[0], physical_bytes(work))
        report['runs'][name] = {'exit_code': proc.returncode, 'seconds': time.time()-started,
                                 'sampled_peak_bytes': peak[0], 'retained_bytes': physical_bytes(work)}
        if name == 'managed':
            state = json.loads((builder.storage_bundle.folder / 'state.json').read_text())
            report['runs'][name]['state'] = {k: state.get(k) for k in ['status', 'error', 'peak_bytes', 'bytes_deleted']}
            report['runs'][name]['estimate'] = builder.storage_bundle.plan['estimated_peak_bytes']
        (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(f'FINISH {name}: rc={proc.returncode}, peak={peak[0]/2**30:.3f} GiB', flush=True)
        if proc.returncode or (name == 'managed' and state.get('status') != 'complete'):
            raise RuntimeError(f'{name} failed; see {work / "cli.log"}')
    base = args.output / 'baseline'
    managed = args.output / 'managed'
    products = sorted(p.relative_to(base) for p in base.rglob('*.fit')
                      if p.name.endswith('_final.fit') or p.name in ('pp_flat_stacked.fit', 'mosaic_final_scaled.fit'))
    for relative in products:
        candidate = managed / relative
        if relative.name == 'pp_flat_stacked.fit' and args.compression == 'gzip2':
            candidate = candidate.with_suffix('.fit.fz')
        report['comparisons'][str(relative)] = compare_images(base / relative, candidate)
        if args.repeat_baseline:
            report['comparisons']['baseline_repeat/' + str(relative)] = compare_images(base / relative, args.output / 'baseline_repeat' / relative)
    report['inputs_unchanged'] = all(module.StorageRuntime.fingerprint(name) == fingerprint for name, fingerprint in before.items())
    (args.output / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
