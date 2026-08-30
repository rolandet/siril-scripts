import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.py"
)
SPEC = importlib.util.spec_from_file_location("siril_v3_mosaic_tests", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _fits_header(cards):
    encoded = b"".join(card.ljust(80).encode("ascii") for card in cards)
    return encoded.ljust(((len(encoded) + 2879) // 2880) * 2880, b" ")


def _normal_fits(width, height):
    return _fits_header([
        "SIMPLE  =                    T",
        "BITPIX  =                   16",
        "NAXIS   =                    2",
        f"NAXIS1  = {width:20d}",
        f"NAXIS2  = {height:20d}",
        "END",
    ])


def _compressed_fits(width, height):
    primary = _fits_header([
        "SIMPLE  =                    T",
        "BITPIX  =                    8",
        "NAXIS   =                    0",
        "EXTEND  =                    T",
        "END",
    ])
    compressed_image = _fits_header([
        "XTENSION= 'BINTABLE'",
        "BITPIX  =                    8",
        "NAXIS   =                    2",
        "NAXIS1  =                    8",
        "NAXIS2  =                   20",
        "PCOUNT  =                    0",
        "GCOUNT  =                    1",
        "ZIMAGE  =                    T",
        "ZNAXIS  =                    2",
        f"ZNAXIS1 = {width:20d}",
        f"ZNAXIS2 = {height:20d}",
        "END",
    ])
    return primary + compressed_image


class FitsGeometryTests(unittest.TestCase):
    def test_reads_standard_image_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "light.fits"
            path.write_bytes(_normal_fits(5496, 3672))
            self.assertEqual(MODULE._read_fits_size_quick(path), (5496, 3672))

    def test_reads_fpack_compressed_image_geometry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "light.fit.fz"
            path.write_bytes(_compressed_fits(5496, 3672))
            self.assertEqual(MODULE._read_fits_size_quick(path), (5496, 3672))

    def test_tries_later_light_when_first_header_is_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.fits"
            good = Path(tmp) / "good.fits"
            bad.write_bytes(b"not a FITS header")
            good.write_bytes(_normal_fits(5496, 3672))
            self.assertEqual(
                MODULE._first_readable_light_geometry([bad, good]),
                (str(good), 5496, 3672),
            )


class FeatherCalculationTests(unittest.TestCase):
    def test_veil_example(self):
        self.assertEqual(MODULE.calculate_mosaic_feather_px(5496, 3672, 12), 220)

    def test_zero_overlap_disables_feathering(self):
        self.assertEqual(MODULE.calculate_mosaic_feather_px(5496, 3672, 0), 0)

    def test_nonzero_overlap_preserves_clamp(self):
        self.assertEqual(MODULE.calculate_mosaic_feather_px(100, 100, 1), 20)
        self.assertEqual(MODULE.calculate_mosaic_feather_px(10000, 10000, 50), 300)


class MosaicCommandTests(unittest.TestCase):
    def test_phase2_stack_uses_calculated_feathering(self):
        project = MODULE.Project(name="Mosaic Test", stack_method="Mean")
        lines = []
        MODULE.emit_phase2_mosaic(
            work=Path("C:/mosaic-test"),
            produced=[
                "C:/mosaic-test/Session 1/P1/process/P1_final.fit",
                "C:/mosaic-test/Session 1/P2/process/P2_final.fit",
            ],
            p=project,
            L=lines,
            comp_state=[0],
            set_comp_if_needed=lambda *_args: None,
            safe_slug=lambda name: name.replace(" ", "_"),
            feather_px=220,
            overlap_norm=True,
        )
        stack_line = next(line for line in lines if line.startswith("stack r_mosaic_"))
        self.assertIn("-feather=220", stack_line)
        self.assertIn("-overlap_norm", stack_line)
        self.assertFalse(any(line.lstrip().lower().startswith("echo ") for line in lines))


if __name__ == "__main__":
    unittest.main()
