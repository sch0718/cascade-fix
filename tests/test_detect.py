import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import detect  # noqa: E402
from helpers import git, make_repo, run_cli, write_repos_toml  # noqa: E402
from test_cli_e2e import FIXTURES, CliTestBase  # noqa: E402

FAKE_GH = f"{sys.executable} {Path(__file__).resolve().parent / 'fake_gh.py'}"


class EstimateTest(unittest.TestCase):
    def test_code_and_doc_counts(self):
        e = detect.estimate(3, "code", "claude")
        self.assertEqual((e["stage1"], e["adjudicator_max"], e["opinion_max"], e["recheck"]), (6, 3, 3, 1))
        self.assertEqual((e["min"], e["max"]), (6, 13))
        e = detect.estimate(2, "doc", "codex")
        self.assertEqual((e["stage1"], e["checker_max"], e["opinion_max"], e["recheck"]), (6, 2, 0, 0))
        self.assertIn("점검자 최대 2", detect.estimate_line(2, "doc", "codex"))


class DetectCliTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"calc.py": "x = 1\n", "README.md": "# r\n", "tests/test_calc.py": "\n"})
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["python3 -m unittest {files}"]}})
        self.env = {"CASCADE_REPOS_TOML": str(self.toml), "CASCADE_REVIEWER": "auto"}

    def detect(self, *args):
        p = run_cli(["detect", "--json", "--no-gh", *args], cwd=self.repo, env=self.env)
        return p.returncode, json.loads(p.stdout)

    def test_clean_repo_has_nothing_to_do(self):
        code, r = self.detect()
        self.assertEqual(code, 6)
        self.assertIn("처리할 것이 없다", r["blockers"][0])

    def test_uncommitted_code_recommends_claude_review(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        (self.repo / "new.py").write_text("y = 1\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(r["uncommitted"]), ["calc.py", "new.py"])
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"], rec["review_args"]), ("claude-code", "code", "--uncommitted"))
        self.assertIn("review --kind code --uncommitted", rec["steps"][0])
        self.assertEqual(r["reviewer"], "claude")

    def test_branch_with_only_docs_recommends_doc_review(self):
        git(self.repo, "switch", "-q", "-c", "feature")
        (self.repo / "README.md").write_text("# 바꿈\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "doc")
        code, r = self.detect()
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["mode"]), ("claude-doc", "doc"))
        self.assertEqual(r["ahead"]["commits"], 1)
        self.assertIn("--doc README.md", rec["steps"][0])

    def test_missing_config_offers_unittest_candidate(self):
        self.env["CASCADE_REPOS_TOML"] = str(self.tmp / "none.toml")
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 0)
        self.assertIn("discover -s tests", r["config"]["candidates"]["tests"][0])
        self.assertTrue(any("검증 명령 등록" in a for a in r["approvals"]))
        p = run_cli(["detect", "--no-gh"], cwd=self.repo, env=self.env)
        self.assertIn("  후보 tests: python3 -m unittest discover -s tests -p 'test_*.py'", p.stdout)

    def test_no_tests_blocks_code_mode(self):
        self.env["CASCADE_REPOS_TOML"] = str(self.tmp / "none.toml")
        git(self.repo, "rm", "-q", "tests/test_calc.py")
        git(self.repo, "commit", "-qm", "rm")
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        code, r = self.detect()
        self.assertEqual(code, 6)
        self.assertTrue(any("테스트 명령을 찾지 못했다" in b for b in r["blockers"]))

    def test_sensitive_path_is_noted(self):
        (self.repo / "auth.py").write_text("t = 1\n", encoding="utf-8")
        _, r = self.detect()
        self.assertTrue(any("auth.py" in n for n in r["notes"]))

    def test_codex_input_is_classified_and_estimated(self):
        code, r = self.detect("--input", str(FIXTURES / "codex-code-e2e.final"), "--reviewer", "codex")
        rec = r["recommendation"]
        self.assertEqual((rec["source"], rec["estimate"]["stage1"]), ("codex-code", 4))
        self.assertIn("--reviewer codex", rec["steps"][0])
        self.assertTrue(any("Codex 전송 동의" in a for a in r["approvals"]))

    def test_pr_with_unresolved_threads_recommends_pr_comment(self):
        env = {**self.env, "CASCADE_GH": FAKE_GH, "CASCADE_FAKE_GH_PR_URL": "https://github.com/o/r/pull/7",
               "CASCADE_FAKE_GH_FILE": str(FIXTURES / "pr-threads.json")}
        p = run_cli(["detect", "--json"], cwd=self.repo, env=env)
        r = json.loads(p.stdout)
        self.assertEqual((r["pr"]["number"], r["pr"]["repo"]), (7, "o/r"))
        self.assertGreater(r["pr"]["unresolved"], 0)
        rec = r["recommendation"]
        self.assertEqual(rec["source"], "pr-comment")
        self.assertIn("--input 7 --pr-repo o/r --me me", rec["steps"][0])

    def test_human_output(self):
        (self.repo / "calc.py").write_text("x = 2\n", encoding="utf-8")
        p = run_cli(["detect", "--no-gh"], cwd=self.repo, env=self.env)
        self.assertIn("추천: --source claude-code", p.stdout)
        self.assertIn("규모: 첫 검토 1개 + 지적 1건당 에이전트 2~4개", p.stdout)
        self.assertIn("ok=예", p.stdout)


class SeverityTest(CliTestBase):
    def test_severity_defers_other_findings_to_hold(self):
        raw = self.tmp / "doc.final"
        raw.write_text(
            "Critical\n1. add가 뺀다\n   위치: calc.py:2\n   근거: a\n\n"
            "Warning\n없음.\n\n"
            "Suggestion\n1. 이름\n   위치: calc.py:5\n   근거: b\n",
            encoding="utf-8")
        p = self.cli("init", "--source", "claude-code", "--input", str(raw), "--severity", "critical,Warning")
        self.assertIn("보류 후보로 둔다", p.stdout)
        self.assertIn("규모: 회차당 에이전트 2~", p.stdout)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        rnd = self.ledger().rounds[0]
        self.assertEqual([f["id"] for f in rnd.findings], ["r1-01"])
        self.assertEqual([f["id"] for f in rnd.hold_findings], ["r1-02"])
        self.assertEqual([(d["finding_id"], d["outcome"]) for d in rnd.decisions], [("r1-02", "보류후보")])

        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        stage1 = self.run_dir / "round-1" / "stage1.json"
        stage1.write_text(json.dumps({"results": []}), encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        outcomes = {d["finding_id"]: d["outcome"] for d in self.ledger().rounds[0].decisions}
        self.assertEqual(outcomes["r1-02"], "보류후보")
        self.assertIn("r1-01", outcomes)

    def test_bad_or_empty_severity_stops(self):
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"),
                     "--severity", "Major", ok=False)
        self.assertIn("--severity", p.stdout)
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"),
                     "--severity", "Critical", ok=False)
        self.assertIn("맞는 지적이 없다", p.stdout)


if __name__ == "__main__":
    unittest.main()
