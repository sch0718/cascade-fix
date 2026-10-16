import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay  # noqa: E402
import score  # noqa: E402
from helpers import git, make_repo, write_repos_toml  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CALC = "def add(a, b):\n    return a - b\n"
TEST_CALC = """import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 0), 1)
"""
FINDING = {"id": "r1-01", "source": "codex-code", "severity": "Warning", "location": "calc.py:2",
           "body": "add가 더하지 않고 뺀다", "evidence": "add(2, 3)이 -1을 돌려준다", "thread_id": None, "outdated": False}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def worker(verdict, role, checker_ok=None):
    return {"finding_id": "r1-01", "role": role, "verdict": verdict, "needs_design_decision": False,
            "reason": "근거", "verify": [], "checker_ok": checker_ok, "model_id": "m", "patch": ""}


def make_run(runs, item_id, decisions, results=None, mode="code", opinions=None, apply=None, error=None):
    base = Path(runs) / item_id
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "meta.json", {"error": error} if error else {"truth": {}, "baseline_opus_calls": 1})
    if error:
        return
    write_json(base / "ledger.json", {"mode": mode, "source": "codex-doc" if mode == "doc" else "codex-code",
                                      "opinions": opinions or {},
                                      "rounds": [{"no": 1, "decisions": decisions}]})
    rnd = base / "run" / "round-1"
    write_json(rnd / "results.json", results or {})
    if apply is not None:
        write_json(rnd / "apply.json", apply)


def decision(outcome, patch_from=None, reasons=None):
    return {"finding_id": "r1-01", "outcome": outcome, "patch_from": patch_from, "reasons": reasons or []}


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)
        self.sample = FIXTURES / "sample.jsonl"

    def three_runs(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")], apply={"final_ok": True})
        make_run(self.runs, "s2",
                 [decision("판가름", "adjudicator", ["패치가 다름", "판가름: 곱셈이 맞다"])],
                 {"r1-01:adjudicator": worker("사실", "adjudicator")})
        make_run(self.runs, "s3", [decision("판단불가", None, ["판가름 못 정함"])],
                 {"r1-01:adjudicator": worker("못 정함", "adjudicator")})

    def test_score_computes_agreement_and_opus_ratio(self):
        self.three_runs()
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["agree"], m["judged"]), (2, 2))
        self.assertEqual((m["opus_calls"], m["baseline_calls"]), (2, 3))
        self.assertAlmostEqual(m["opus_ratio"], 2 / 3)
        self.assertEqual(m["paths"], {"합의": 1, "판가름": 1, "Codex 검토": 0, "사람": 1})
        self.assertEqual(m["patch_differs"], 1)
        self.assertEqual(m["items"], 3)
        self.assertEqual(m["regressions"], 0)
        text = score.render(m)
        self.assertIn("2/2", text)
        self.assertIn("66.7%", text)
        self.assertIn("| 패치 다르지만 둘 다 통과 | 1/3 (33.3%) |", text)

    def test_score_prints_pass_fail_line(self):
        self.three_runs()
        out = self.runs / "score.md"
        p = subprocess.run([sys.executable, str(ROOT / "eval" / "score.py"), "--runs", str(self.runs),
                            "--sample", str(self.sample), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        line = "성공 기준: 판정 일치 100.0% (목표 95%) 통과 · Opus 호출 66.7% (목표 40% 이하) 미달 · 회귀 0건 (목표 0) 통과"
        self.assertIn(line, p.stdout)
        self.assertIn(line, out.read_text(encoding="utf-8"))

    def test_dry_run_only_runs_report_regression_as_unmeasured(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")], apply={"dry_run": True, "applied": []})
        make_run(self.runs, "s3", [decision("합의-아님")], apply={"dry_run": True, "applied": []})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertFalse(m["regression_measured"])
        segment = score.verdict_line(m).split(" · ")[-1]
        self.assertIn("회귀 미측정", segment)
        self.assertNotIn("통과", segment)
        self.assertNotIn("미달", segment)
        self.assertIn("| 회귀 건수 | 측정 불가(dry-run은 전체 테스트를 돌리지 않음) |", score.render(m))

    def test_incomplete_items_are_excluded_and_counted(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")])
        make_run(self.runs, "s2", [decision("합의-사실", "a")])
        write_json(self.runs / "s2" / "meta.json", {"incomplete": True})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["incomplete"], m["items"], m["baseline_calls"]), (1, 1, 1))
        self.assertIn("| 미완료 항목 수 | 1 |", score.render(m))

    def test_fail_line_when_regression_and_low_agreement(self):
        make_run(self.runs, "s1", [decision("합의-아님")], apply={"final_ok": False})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["agree"], m["judged"]), (0, 1))
        self.assertEqual(m["regressions"], 1)
        line = score.verdict_line(m)
        self.assertIn("판정 일치 0.0% (목표 95%) 미달", line)
        self.assertIn("회귀 1건 (목표 0) 미달", line)
        self.assertIn("Opus 호출 0.0% (목표 40% 이하) 통과", line)

    def test_error_items_are_counted_and_skipped(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")])
        make_run(self.runs, "s2", [], error="boom")
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["errors"], m["items"], m["baseline_calls"]), (1, 1, 1))

    def test_adjudicated_no_and_unreproducible_and_codex_paths(self):
        make_run(self.runs, "s3", [decision("판가름", None, ["판가름: 아님", "판가름: 쓰임이 없다"])])
        make_run(self.runs, "s2", [decision("판단불가", None, ["판가름 재현 불가"])], opinions={"r1-01": "의견"})
        make_run(self.runs, "s1", [decision("설계결정")])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["predicted"], {"s1": "판정 없음", "s2": "재현 불가", "s3": "아님"})
        self.assertEqual((m["agree"], m["judged"]), (1, 2))
        self.assertEqual(m["paths"], {"합의": 0, "판가름": 1, "Codex 검토": 1, "사람": 1})

    def test_doc_checker_accuracy(self):
        make_run(self.runs, "s1", [decision("판가름", "writer", ["점검자 아니오", "판가름: 고친다"])],
                 {"r1-01:checker": worker("사실", "checker", checker_ok=False),
                  "r1-01:adjudicator": worker("사실", "adjudicator")}, mode="doc")
        make_run(self.runs, "s2", [decision("판가름", None, ["점검자 아니오", "판가름: 아님"])],
                 {"r1-01:checker": worker("사실", "checker", checker_ok=False),
                  "r1-01:adjudicator": worker("아님", "adjudicator")}, mode="doc")
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["checker"], (1, 2))
        self.assertIn("| 문서 점검자 정확도 | 1/2 (50.0%) |", score.render(m))

    def test_checker_accuracy_dash_without_doc_items(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertIn("| 문서 점검자 정확도 | - |", score.render(m))


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"calc.py": CALC, "test_calc.py": TEST_CALC, ".gitignore": "__pycache__/\n"})
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.toml = write_repos_toml(self.tmp, {str(self.repo): {
            "tests": ["python3 -m unittest {files}"],
            "full_tests": ["python3 -m unittest discover -s ."],
            "format": [],
        }})
        self.out = self.tmp / "out"

    def item(self, item_id, commit=None, truth="아님"):
        return {"id": item_id, "repo": str(self.repo), "commit": commit or self.head, "source": "codex-code",
                "doc": None, "finding": FINDING, "truth": {"verdict": truth, "accepted_patch": None},
                "baseline_opus_calls": 1}

    def write_sample(self, items):
        path = self.tmp / "sample.jsonl"
        path.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
        return path

    def write_fake(self, item_id):
        fake = self.tmp / "fake" / item_id
        write_json(fake / "stage1.json", {"results": [
            {"id": f"r1-01:{r}", "output": {"verdict": "아님", "needs_design_decision": False, "reason": "쓰임이 없다",
                                            "verify": [], "model_id": m}}
            for r, m in (("a", "claude-sonnet-5-5"), ("b", "claude-haiku-5-5"))]})
        return self.tmp / "fake"

    def run_replay(self, sample, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            return replay.main(["--sample", str(sample), "--out", str(self.out), "--repos-toml", str(self.toml), *extra])

    def test_replay_uses_fake_results_and_writes_ledgers(self):
        sample = self.write_sample([self.item("item-01")])
        fake = self.write_fake("item-01")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 0)
        base = self.out / "item-01"
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertTrue(ledger["dry_run"])
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["truth"], {"verdict": "아님", "accepted_patch": None})
        self.assertEqual(meta["baseline_opus_calls"], 1)
        self.assertNotIn("error", meta)
        self.assertTrue((base / "run" / "round-1" / "results.json").is_file())
        self.assertTrue((base / "run" / "report.md").is_file())
        self.assertFalse((base / "wt").exists())
        listed = git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual(listed.count("worktree "), 1)
        self.assertEqual(git(self.repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["agree"], m["judged"]), (1, 1))

    def test_replay_worktree_add_skips_user_hooks(self):
        marker = self.tmp / "hook-ran"
        hook = self.repo / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        sample = self.write_sample([self.item("item-01")])
        self.assertEqual(self.run_replay(sample, "--fake-results", str(self.write_fake("item-01"))), 0)
        self.assertFalse(marker.exists())

    def test_replay_records_error_and_continues(self):
        sample = self.write_sample([self.item("bad", commit="deadbeef" * 5), self.item("item-02")])
        fake = self.write_fake("item-02")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 1)
        meta = json.loads((self.out / "bad" / "meta.json").read_text(encoding="utf-8"))
        self.assertIn("error", meta)
        self.assertTrue((self.out / "item-02" / "ledger.json").is_file())
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["errors"], m["items"]), (1, 1))

    def test_replay_limit(self):
        sample = self.write_sample([self.item("item-01"), self.item("item-02")])
        fake = self.write_fake("item-01")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake), "--limit", "1"), 0)
        self.assertTrue((self.out / "item-01").is_dir())
        self.assertFalse((self.out / "item-02").exists())

    def test_replay_without_fake_results_stops_after_jobs_and_collect_finishes(self):
        sample = self.write_sample([self.item("item-01")])
        self.assertEqual(self.run_replay(sample), 0)
        base = self.out / "item-01"
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        run_dir = Path(meta["pending_run_dir"])
        self.assertTrue((run_dir / "round-1" / "stage1-args.json").is_file())
        self.assertTrue((base / "wt").is_dir())

        self.assertEqual(self.run_replay(sample, "--collect"), 0)
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertIs(meta["incomplete"], True)
        self.assertIn("pending_run_dir", meta)
        self.assertFalse((base / "ledger.json").exists())
        self.assertTrue((base / "wt").is_dir())
        m = score.compute(self.out, score.load_sample(sample))
        self.assertEqual((m["incomplete"], m["items"]), (1, 0))

        write_json(run_dir / "round-1" / "apply.json", {"dry_run": True, "applied": []})
        self.assertEqual(self.run_replay(sample, "--collect"), 0)
        meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
        self.assertNotIn("pending_run_dir", meta)
        self.assertNotIn("incomplete", meta)
        self.assertTrue((base / "ledger.json").is_file())
        self.assertFalse((base / "wt").exists())
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_replay_rejects_reused_worktree_at_other_commit(self):
        (self.repo / "calc.py").write_text(CALC + "\n", encoding="utf-8")
        git(self.repo, "commit", "-qam", "second")
        git(self.repo, "worktree", "add", "--detach", str(self.out / "item-01" / "wt"), self.head)
        sample = self.write_sample([self.item("item-01", commit=git(self.repo, "rev-parse", "HEAD"))])
        self.assertEqual(self.run_replay(sample, "--fake-results", str(self.write_fake("item-01"))), 1)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        self.assertIn("HEAD", meta["error"])
        self.assertEqual(git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 1)


if __name__ == "__main__":
    unittest.main()
