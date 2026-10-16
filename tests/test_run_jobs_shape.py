import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


class RunJobsShapeTest(unittest.TestCase):
    def setUp(self):
        self.src = (SKILL / "workflows/run-jobs.js").read_text()

    def test_script_has_meta_and_uses_args_jobs(self):
        self.assertIn("export const meta", self.src)
        self.assertIn("args.jobs", self.src)
        self.assertNotIn("Date.now", self.src)

    def test_script_has_no_nondeterministic_calls(self):
        self.assertNotIn("Math.random", self.src)
        self.assertNotIn("new Date", self.src)


if __name__ == "__main__":
    unittest.main()
