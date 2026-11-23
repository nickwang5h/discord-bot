import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "healthcheck.py"
spec = importlib.util.spec_from_file_location("healthcheck", SCRIPT)
healthcheck = importlib.util.module_from_spec(spec)
spec.loader.exec_module(healthcheck)


class PythonVersionCheckTests(unittest.TestCase):
    # The code uses asyncio.timeout and datetime.UTC, both new in Python 3.11.
    def check(self, version_info):
        report = healthcheck.Report()
        healthcheck.check_python(report, version_info)
        return report

    def test_python_3_10_is_an_error(self):
        report = self.check((3, 10, 14, "final", 0))
        self.assertEqual(report.errors, ["需要 Python 3.11+"])
        self.assertEqual(report.successes, [])

    def test_python_3_11_passes(self):
        report = self.check((3, 11, 0, "final", 0))
        self.assertEqual(report.errors, [])
        self.assertEqual(len(report.successes), 1)


if __name__ == "__main__":
    unittest.main()
