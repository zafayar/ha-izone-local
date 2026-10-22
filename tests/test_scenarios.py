"""Runs each offline scenario: a fake bridge, and stand-ins for Home Assistant.

    python -m unittest discover -s tests -v     (needs aiohttp)

Each scenario is a script that prints PASS/FAIL lines and "ALL PASSED".
"""

import os
import pathlib
import subprocess
import sys
import unittest

SCENARIOS = sorted((pathlib.Path(__file__).parent / "scenarios").glob("*.py"))


class Scenarios(unittest.TestCase):
    def test_scenarios(self) -> None:
        self.assertTrue(SCENARIOS)
        for path in SCENARIOS:
            with self.subTest(path.stem):
                result = subprocess.run(
                    [sys.executable, str(path)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                failed = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL")]
                detail = "\n".join(failed) or result.stderr[-3000:]
                if "ALL PASSED" not in result.stdout and os.environ.get("GITHUB_ACTIONS"):
                    # Annotations show on the run page without opening the log.
                    message = detail.replace("%", "%25").replace("\n", "%0A")
                    print(f"::error title={path.stem} failed::{message}", flush=True)
                self.assertIn("ALL PASSED", result.stdout, detail)


if __name__ == "__main__":
    unittest.main()
