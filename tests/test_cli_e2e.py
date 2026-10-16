import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade.rounds import Ledger, RoundRecord  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from helpers import git, make_repo, run_cli, write_repos_toml  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CALC = """def add(a, b):
    return a - b


def ident(x):
    return x


def neg(x):
    return -x


def mul(a, b):
    return a + b
"""
TEST_CALC = """import unittest

from calc import ident


class IdentTest(unittest.TestCase):
    def test_ident(self):
        self.assertEqual(ident(3), 3)
"""
RECHECK_RESULT = """## 해소 여부
- r1-01: 해소됨 — add가 더한다
- r1-02: 해소됨 — mul이 곱한다

Critical
없음.

Warning
없음.

Suggestion
1. ident에 타입 힌트가 없다
   위치: calc.py:5
   근거: 다른 함수와 맞지 않는다
   제안: 힌트를 단다
"""


def fix(text, old, new):
    assert old in text, old
    return text.replace(old, new)


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {
            "calc.py": CALC,
            "test_calc.py": TEST_CALC,
            ".gitignore": "__pycache__/\n",
        })
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -m unittest discover -s ."],
            "format": [],
        }})
        self.env = {"CASCADE_REPOS_TOML": str(toml), "PYTHONDONTWRITEBYTECODE": "1"}

    def cli(self, *args, ok=True):
        p = run_cli(list(args), cwd=self.repo, env=self.env)
        if ok:
            self.assertEqual(p.returncode, 0, f"{args}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        return p

    def ledger(self):
        return Ledger.load(self.run_dir / "ledger.json")

    def wt(self, name):
        return Path(self.ledger().snapshot["worktrees"][name])

    def init(self, *extra):
        p = self.cli("init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final"), *extra)
        line = next(x for x in p.stdout.splitlines() if x.startswith("run_dir="))
        self.run_dir = Path(line.split("=", 1)[1])
        return p

    def run_to_apply(self, *init_extra):
        self.commits_before = git(self.repo, "rev-list", "--count", "HEAD")
        self.init(*init_extra)
        findings = json.loads((self.run_dir / "round-1" / "findings.json").read_text(encoding="utf-8"))
        self.assertEqual([f["id"] for f in findings], ["r1-01", "r1-02"])
        self.assertEqual([f["severity"] for f in findings], ["Warning", "Warning"])

        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        args = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(j["id"], j["model"]) for j in args["jobs"]],
            [("r1-01:a", "claude-sonnet-5-5"), ("r1-01:b", "claude-haiku-5-5"),
             ("r1-02:a", "claude-sonnet-5-5"), ("r1-02:b", "claude-haiku-5-5")],
        )
        self.assertTrue(self.wt("base").is_dir())

        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            self.assertIsNotNone(commit_worktree(wt, f"cascade-fix {name}"))
        wt = self.wt("r1-02-a")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 a")

        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        self.assertIn("판가름", p.stdout)
        decisions = json.loads((self.run_dir / "round-1" / "decisions.json").read_text(encoding="utf-8"))
        outcomes = {d["finding_id"]: d["outcome"] for d in decisions}
        self.assertEqual(outcomes, {"r1-01": "합의-사실", "r1-02": "판가름"})
        adj_args = json.loads((self.run_dir / "round-1" / "adj-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["model"]) for j in adj_args["jobs"]], [("r1-02:adjudicator", "claude-opus-5-5")])
        self.assertTrue((self.run_dir / "round-1" / "patch-r1-01-a.diff").read_text(encoding="utf-8").strip())

        wt = self.wt("r1-02-adj")
        (wt / "calc.py").write_text(fix(CALC, "return a + b", "return a * b"), encoding="utf-8")
        commit_worktree(wt, "cascade-fix r1-02 adjudicator")
        adj = self.run_dir / "round-1" / "adj.json"
        shutil.copyfile(FIXTURES / "workflow-adj.json", adj)
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        decisions = {d["finding_id"]: d for d in self.ledger().rounds[0].decisions}
        self.assertEqual(decisions["r1-02"]["outcome"], "판가름")
        self.assertEqual(decisions["r1-02"]["patch_from"], "adjudicator")
        self.assertTrue((self.run_dir / "round-1" / "patch-r1-02-adj.diff").is_file())

        return self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")


class CliEndToEndTest(CliTestBase):
    def test_full_run_applies_both_fixes_and_ends_after_recheck(self):
        self.run_to_apply()
        calc = (self.repo / "calc.py").read_text(encoding="utf-8")
        self.assertIn("return a + b\n\n\ndef ident", calc)
        self.assertIn("return a * b", calc)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), self.commits_before)
        self.assertEqual(git(self.repo, "diff", "--cached", "--name-only"), "")
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual(apply["applied"], ["r1-01", "r1-02"])
        self.assertTrue(apply["final_ok"])
        self.assertEqual(self.ledger().rounds[0].adopted, ["r1-01", "r1-02"])

        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| 1 | 2 | 1 | 1 | 0 | 0 |", p.stdout)
        self.assertNotIn("적용하지 않음(dry-run)", p.stdout)
        self.assertTrue((self.run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8").startswith("fix: 2건"))

        prompt = self.run_dir / "round-1" / "recheck-prompt.md"
        self.assertTrue(prompt.is_file())
        self.assertIn("return a * b", prompt.read_text(encoding="utf-8"))
        self.assertTrue(self.ledger().rounds[0].recheck_opened)

        result = self.tmp / "recheck.final"
        result.write_text(RECHECK_RESULT, encoding="utf-8")
        self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(result))
        ledger = self.ledger()
        self.assertEqual(ledger.status, "종료")
        self.assertEqual(len(ledger.rounds), 1)
        hold = [d for d in ledger.rounds[0].decisions if d["outcome"] == "보류후보"]
        self.assertEqual(len(hold), 1)
        held = next(f for f in ledger.rounds[0].findings if f["id"] == hold[0]["finding_id"])
        self.assertEqual(held["severity"], "Suggestion")

        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("ident에 타입 힌트가 없다", p.stdout)
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("cleanup", p.stdout)

        self.cli("cleanup", "--run-dir", str(self.run_dir))
        self.assertEqual(git(self.repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertTrue((self.run_dir / "ledger.json").is_file())
        self.assertFalse((self.run_dir / "wt").exists())

    def test_dry_run_leaves_working_tree_and_shows_banner(self):
        self.run_to_apply("--dry-run")
        self.assertEqual((self.repo / "calc.py").read_text(encoding="utf-8"), CALC)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.ledger().rounds[0].adopted, ["r1-01", "r1-02"])
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("**적용하지 않음(dry-run)**", p.stdout)
        self.assertIn("| 1 | 2 | 1 | 1 | 0 | 0 |", p.stdout)

    def test_completed_step_is_refused_without_force(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--force", p.stdout + p.stderr)

    def test_status_names_next_command(self):
        self.init()
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("jobs --run-dir", p.stdout)
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.cli("status", "--run-dir", str(self.run_dir))
        self.assertIn("stage1-args.json", p.stdout)
        self.assertIn("judge", p.stdout)

    def test_apply_failure_moves_finding_to_undecidable(self):
        self.run_to_apply_with_conflict()
        decisions = {d["finding_id"]: d for d in self.ledger().rounds[0].decisions}
        self.assertEqual(decisions["r1-01"]["outcome"], "판단불가")
        self.assertTrue(any(r.startswith("패치 적용 실패: ") for r in decisions["r1-01"]["reasons"]))
        self.assertNotIn("r1-01", self.ledger().rounds[0].adopted)

    def test_broken_full_suite_marks_interference_and_keeps_tree(self):
        toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -c 'import sys; sys.exit(3)'"],
        }})
        self.env["CASCADE_REPOS_TOML"] = str(toml)
        self.run_to_apply()
        apply = json.loads((self.run_dir / "round-1" / "apply.json").read_text(encoding="utf-8"))
        self.assertEqual(apply["applied"], ["r1-01", "r1-02"])
        self.assertIs(apply["final_ok"], False)
        self.assertIs(apply["interference"], True)
        ledger = self.ledger()
        for d in ledger.rounds[0].decisions:
            self.assertEqual(d["outcome"], "판단불가")
            self.assertIn("전체 테스트 실패(간섭 의심)", d["reasons"])
        self.assertEqual(ledger.rounds[0].adopted, [])
        self.assertEqual(ledger.status, "종료")
        self.assertIn("return a * b", (self.repo / "calc.py").read_text(encoding="utf-8"))
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("## 간섭 의심", p.stdout)
        self.assertIn("되돌리지 않았다", p.stdout)
        self.assertEqual((self.run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8"), "")


    def run_to_apply_with_conflict(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.run_dir / "round-1" / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        (self.repo / "calc.py").write_text(fix(CALC, "return a - b", "return b - a"), encoding="utf-8")
        adj = self.run_dir / "round-1" / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        self.assertTrue((self.run_dir / "round-1" / "opinion-r1-02.md").is_file())
        opinion = self.tmp / "opinion.final"
        opinion.write_text("Opus와 같은 의견이다", encoding="utf-8")
        self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(opinion))
        self.assertEqual(self.ledger().opinions["r1-02"], "Opus와 같은 의견이다")
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")


class CliDocModeTest(CliTestBase):
    def test_doc_mode_runs_checker_loop_and_applies_writer_patch(self):
        plan = "# 계획\n\n1단계는 `calc.py`를 고친다.\n"
        (self.repo / "plan.md").write_text(plan, encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), "--doc", str(self.repo / "plan.md"))
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        self.assertEqual(self.ledger().docs, ["plan.md"])
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        args = json.loads((self.run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in args["jobs"]], ["r1-01:a", "r1-01:b", "r1-01:writer"])

        wt = self.wt("r1-01-writer")
        (wt / "plan.md").write_text(plan.replace("고친다", "고쳐 add가 더하게 한다"), encoding="utf-8")
        commit_worktree(wt, "writer")
        out = {"verdict": "사실", "needs_design_decision": False, "reason": "모호하다", "verify": []}
        stage1 = self.run_dir / "round-1" / "stage1.json"
        stage1.write_text(json.dumps({"results": [
            {"id": "r1-01:a", "output": {**out, "model_id": "claude-sonnet-5-5"}},
            {"id": "r1-01:b", "output": {**out, "model_id": "claude-haiku-5-5"}},
            {"id": "r1-01:writer", "output": {**out, "model_id": "claude-sonnet-5-5"}},
        ]}, ensure_ascii=False), encoding="utf-8")
        p = self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        checker_args = json.loads((self.run_dir / "round-1" / "checker-args.json").read_text(encoding="utf-8"))
        self.assertEqual([j["id"] for j in checker_args["jobs"]], ["r1-01:checker"])
        self.assertIn("checker.json", p.stdout)
        self.assertFalse((self.run_dir / "round-1" / "decisions.json").exists())

        checker = self.run_dir / "round-1" / "checker.json"
        checker.write_text(json.dumps({"results": [{"id": "r1-01:checker", "output": {
            "verdict": "사실", "needs_design_decision": False, "reason": "해소한다", "checker_ok": True,
            "model_id": "claude-haiku-5-5"}}]}, ensure_ascii=False), encoding="utf-8")
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1),
                 "--checker-results", str(checker))
        decisions = self.ledger().rounds[0].decisions
        self.assertEqual([(d["outcome"], d["patch_from"]) for d in decisions], [("합의-사실", "writer")])
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("add가 더하게", (self.repo / "plan.md").read_text(encoding="utf-8"))
        self.assertTrue((self.run_dir / "round-1" / "recheck-prompt.md").is_file())


class CliPrCommentTest(CliTestBase):
    def test_pr_comment_init_uses_gh_from_env(self):
        env = {**self.env, "CASCADE_GH": f"{sys.executable} {Path(__file__).resolve().parent / 'fake_gh.py'}",
               "CASCADE_FAKE_GH_FILE": str(FIXTURES / "pr-threads.json")}
        p = run_cli(["init", "--source", "pr-comment", "--input", "7", "--pr-repo", "o/r", "--me", "sch0718"],
                    cwd=self.repo, env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = Ledger.load(run_dir / "ledger.json")
        self.assertEqual(ledger.source, "pr-comment")
        self.assertEqual(ledger.mode, "code")
        self.assertEqual([f["location"].split(":")[0] for f in ledger.rounds[0].findings], ["src/b.py", "src/d.py"])
        self.assertTrue(all(f["thread_id"] for f in ledger.rounds[0].findings))

    def test_pr_comment_without_config_exits_4(self):
        p = run_cli(["init", "--source", "pr-comment", "--input", "7", "--pr-repo", "o/r"],
                    cwd=self.repo, env={"CASCADE_REPOS_TOML": str(self.tmp / "none.toml")})
        self.assertEqual(p.returncode, 4)


class CliInitTest(CliTestBase):
    def test_zero_findings_warns_and_ends(self):
        raw = self.tmp / "empty.final"
        raw.write_text("검토 결과\n- [P2] 위치에 공백 — some file.py:3\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-code", "--input", str(raw))
        self.assertIn("지적 0건 — 원문을 확인하라", p.stdout)
        self.assertIn("검토 결과", p.stdout)
        self.assertIn("[P", p.stdout)
        self.assertIn("status=종료", p.stdout)
        self.assertFalse((self.repo / ".cascade-fix").exists())

    def test_doc_parse_error_exits_5_with_line(self):
        raw = self.tmp / "doc.final"
        raw.write_text("Critical\n1. 위치 없는 항목\n   근거: 없다\n", encoding="utf-8")
        p = self.cli("init", "--source", "codex-doc", "--input", str(raw), ok=False)
        self.assertEqual(p.returncode, 5)
        self.assertIn("줄 2", p.stdout + p.stderr)

    def test_broken_toml_exits_5(self):
        bad = self.tmp / "bad.toml"
        bad.write_text("[repos\n", encoding="utf-8")
        p = run_cli(["init", "--source", "codex-code", "--input", str(FIXTURES / "codex-code-e2e.final")],
                    cwd=self.repo, env={"CASCADE_REPOS_TOML": str(bad)})
        self.assertEqual(p.returncode, 5)

    def test_codex_doc_runs_without_repo_config(self):
        doc = self.tmp / "plan.md"
        doc.write_text("# 계획\n\n1단계는 `calc.py`를 고친다.\n", encoding="utf-8")
        raw = self.tmp / "doc.final"
        raw.write_text("Warning\n1. 단계가 모호하다\n   위치: 외부/plan.md:3\n   근거: 무엇을 고치는지 없다\n", encoding="utf-8")
        p = run_cli(["init", "--source", "codex-doc", "--input", str(raw), "--doc", str(doc)],
                    cwd=self.repo, env={"CASCADE_REPOS_TOML": str(self.tmp / "none.toml")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        ledger = Ledger.load(run_dir / "ledger.json")
        self.assertEqual(ledger.mode, "doc")
        self.assertEqual(ledger.docs, ["외부/plan.md"])
        self.assertIn("외부/plan.md", git(self.repo, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", ledger.snapshot["commit"]))


class CliResumeTest(unittest.TestCase):
    def test_resume_opens_final_round_with_chosen_findings(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run"
            held = [
                {"id": "r2-01", "source": "codex-code", "severity": "Suggestion", "location": "calc.py:5",
                 "body": "힌트", "evidence": "", "thread_id": None, "outdated": False},
                {"id": "r2-02", "source": "codex-code", "severity": "Warning", "location": "calc.py:9",
                 "body": "부호", "evidence": "", "thread_id": None, "outdated": False},
            ]
            rnd = RoundRecord(
                no=1, findings=held,
                decisions=[{"finding_id": f["id"], "outcome": "보류후보", "patch_from": None, "reasons": []} for f in held],
                adopted=[], recheck_opened=True,
            )
            Ledger(run_id="cf-x", source="codex-code", mode="code", root=td, snapshot={}, dry_run=False,
                   rounds=[rnd], status="종료").save(run_dir / "ledger.json")
            choices = Path(td) / "choices.json"
            choices.write_text(json.dumps({"r2-01": "보류", "r2-02": "지금 반영"}, ensure_ascii=False), encoding="utf-8")
            p = run_cli(["resume", "--run-dir", str(run_dir), "--choices", str(choices)], cwd=td)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            ledger = Ledger.load(run_dir / "ledger.json")
            self.assertEqual(ledger.hold_choices, {"r2-01": "보류", "r2-02": "지금 반영"})
            self.assertEqual(ledger.status, "진행")
            new = ledger.rounds[-1]
            self.assertEqual((new.no, new.final), (2, True))
            self.assertEqual([f["id"] for f in new.findings], ["r2-02"])
            written = json.loads((run_dir / "round-2" / "findings.json").read_text(encoding="utf-8"))
            self.assertEqual([f["id"] for f in written], ["r2-02"])

            bad = Path(td) / "bad.json"
            bad.write_text(json.dumps({"r9-01": "지금 반영"}, ensure_ascii=False), encoding="utf-8")
            p = run_cli(["resume", "--run-dir", str(run_dir), "--choices", str(bad)], cwd=td)
            self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
