import json
import re
import sys
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from cascade import jobs  # noqa: E402

SCHEMAS_BLOCK = re.compile(r"const SCHEMAS = (\{.*?\n\})\n// SCHEMAS 끝", re.S)


class RunJobsShapeTest(unittest.TestCase):
    def setUp(self):
        self.src = (SKILL / "scripts/run-jobs.js").read_text()

    def test_script_has_meta_and_uses_args_jobs(self):
        self.assertIn("export const meta", self.src)
        self.assertIn("args.jobs", self.src)
        self.assertNotIn("Date.now", self.src)

    def test_script_has_no_nondeterministic_calls(self):
        self.assertNotIn("Math.random", self.src)
        self.assertNotIn("new Date", self.src)

    def test_script_schemas_match_python_by_kind(self):
        m = SCHEMAS_BLOCK.search(self.src)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1)), jobs.SCHEMAS)
        self.assertEqual(set(jobs.SCHEMAS), {"worker", "checker", "adjudicator", "review"})

    def test_script_reads_prompt_from_file_path(self):
        self.assertIn("SCHEMAS[j.kind]", self.src)
        self.assertIn("다음 파일을 Read로 읽고 그 지시를 따른다: ${j.promptPath}", self.src)
        self.assertNotIn("j.prompt,", self.src)
        self.assertNotIn("j.schema", self.src)


if __name__ == "__main__":
    unittest.main()
