import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_SCRIPT = ROOT / "osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py"
DISTRIBUTION_SCRIPT = ROOT / "Rolandet-OSC-Stacking.py"


class ScriptSynchronizationTests(unittest.TestCase):
    def test_distribution_script_matches_active_development_script(self):
        development = DEVELOPMENT_SCRIPT.read_text(encoding="utf-8").splitlines()
        distribution = DISTRIBUTION_SCRIPT.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            distribution,
            development,
            "Rolandet-OSC-Stacking.py must stay source-equivalent to the active v3.0.1 script",
        )


if __name__ == "__main__":
    unittest.main()
