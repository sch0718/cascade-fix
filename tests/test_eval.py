import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay  # noqa: E402
import score  # noqa: E402
from cascade.consensus import REASON_BOTH_PASS, REASON_SENSITIVE  # noqa: E402
from fake_cli import fenced, result_json  # noqa: E402
from helpers import git, make_repo, run_cli, write_repos_toml  # noqa: E402
from test_cli_workers import LIMIT_ENTRY, WorkersTestBase  # noqa: E402

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


def make_run(runs, item_id, decisions, results=None, mode="code", opinions=None, apply=None, error=None,
             events=None, files=None):
    base = Path(runs) / item_id
    base.mkdir(parents=True, exist_ok=True)
    write_json(base / "meta.json", {"error": error} if error else {"truth": {}, "baseline_opus_calls": 1})
    if error:
        return
    write_json(base / "ledger.json", {"mode": mode, "source": "codex-doc" if mode == "doc" else "codex-code",
                                      "opinions": opinions or {},
                                      "rounds": [{"no": 1, "decisions": decisions, "events": events or []}]})
    rnd = base / "run" / "round-1"
    write_json(rnd / "results.json", results or {})
    for name, data in (files or {}).items():
        write_json(rnd / name, data)
    if apply is not None:
        write_json(rnd / "apply.json", apply)


def decision(outcome, patch_from=None, reasons=None):
    return {"finding_id": "r1-01", "outcome": outcome, "patch_from": patch_from, "reasons": reasons or []}


def claude_job(job_id, model):
    return {"id": job_id, "label": job_id, "model": model, "effort": "low", "kind": "worker", "promptPath": "p.md"}


def jobs_file(*jobs):
    return {"jobs": [claude_job(*j) for j in jobs]}


def workers_event(stage, counts, usage=None, fallback=None):
    return {"step": f"workers-{stage}", "counts": counts, "usage": usage or {}, "fallback": fallback or []}


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
        self.assertEqual(m["paths"], {"합의": 1, "규칙 채택": 0, "판가름": 1, "Codex 검토": 0, "사람": 1})
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
        self.assertEqual(m["paths"], {"합의": 0, "규칙 채택": 0, "판가름": 1, "Codex 검토": 1, "사람": 1})

    def test_adopt_on_both_pass_counts_rule_path_and_patch_differs(self):
        make_run(self.runs, "s1", [decision("합의-사실", "b", [f"{REASON_BOTH_PASS}: 작은 패치 b 채택"])])
        make_run(self.runs, "s2", [decision("판단불가", None, [f"{REASON_BOTH_PASS}: 크기가 같아 a 채택", REASON_SENSITIVE])])
        make_run(self.runs, "s3", [decision("합의-아님")])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["paths"], {"합의": 1, "규칙 채택": 1, "판가름": 0, "Codex 검토": 0, "사람": 1})
        self.assertEqual(m["patch_differs"], 2)
        self.assertEqual(m["predicted"], {"s1": "사실", "s2": "판정 없음", "s3": "아님"})
        text = score.render(m)
        self.assertIn("| 채택 경로 분포 | 합의 1, 규칙 채택 1, 판가름 0, Codex 검토 0, 사람 1 |", text)
        self.assertIn("| 패치 다르지만 둘 다 통과 | 2/3 (66.7%) |", text)

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

    def test_score_counts_claude_calls_and_fallbacks(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")],
                 events=[workers_event("stage1", {"ok": 1, "limit": 1}, {"codex": {"input": 11, "output": 7, "cache_read": 3}},
                                       ["r1-01:b"])],
                 files={"stage1-args.json": jobs_file(),
                        "stage1-cli-args.json": {"jobs": [{"id": "r1-01:a"}, {"id": "r1-01:b"}]},
                        "stage1-fallback-args.json": jobs_file(("r1-01:b", "claude-haiku-5-5")),
                        "adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["claude_calls"], {"sonnet": 0, "haiku": 1, "opus": 1})
        self.assertEqual(m["fallback"], {"한도": 1})
        self.assertEqual((m["cli_jobs"], m["invalid"]), (2, 0))
        self.assertEqual(m["usage"], {"codex": {"input": 11, "output": 7, "cache_read": 3}})

    def test_score_counts_claude_worker_jobs_by_model_name(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")],
                 files={"stage1-args.json": jobs_file(("r1-01:a", "claude-sonnet-5-5"), ("r1-01:b", "claude-haiku-5-5")),
                        "checker-args.json": jobs_file(("r1-01:checker", "claude-haiku-5-5")),
                        "adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))})
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual(m["claude_calls"], {"sonnet": 1, "haiku": 2, "opus": 1})
        self.assertEqual((m["fallback"], m["cli_jobs"], m["invalid"], m["usage"]), ({}, 0, 0, {}))

    def test_score_uses_last_workers_event_per_stage_and_sums_invalid_and_usage(self):
        usage = {"cursor": {"input": 100, "output": 20, "cache_read": 5}}
        make_run(self.runs, "s1", [decision("합의-사실", "a")], events=[
            workers_event("stage1", {"ok": 1, "error": 1}, usage, ["r1-01:b"]),
            workers_event("stage1", {"ok": 1, "invalid": 1}, usage, ["r1-01:b"]),
            workers_event("checker", {"ok": 1, "auth": 1, "skipped": 1}, usage, ["r1-01:b", "r1-01:c"])])
        m = score.compute(self.runs, score.load_sample(self.sample))
        self.assertEqual((m["cli_jobs"], m["invalid"]), (5, 1))
        self.assertEqual(m["fallback"], {"무효": 1, "로그인": 1, "건너뜀": 1})
        self.assertEqual(m["usage"], {"cursor": {"input": 200, "output": 40, "cache_read": 10}})

    def test_render_without_workers_keeps_table_unchanged(self):
        self.three_runs()
        text = score.render(score.compute(self.runs, score.load_sample(self.sample)))
        self.assertNotIn("대체 실행", text)
        self.assertNotIn("Claude 호출", text)

    def test_render_with_cli_jobs_adds_workers_rows(self):
        make_run(self.runs, "s1", [decision("합의-사실", "a")],
                 events=[workers_event("stage1", {"ok": 3, "invalid": 1}, {"codex": {"input": 1, "output": 2, "cache_read": 3}})],
                 files={"adj-args.json": jobs_file(("r1-01:adjudicator", "claude-opus-5-5"))})
        text = score.render(score.compute(self.runs, score.load_sample(self.sample)))
        self.assertIn("| 대체 실행 | 1/4 (25.0%) — 무효 1 |", text)
        self.assertIn("| 무효 결과 | 1/4 (25.0%) |", text)
        self.assertIn("| Claude 호출 | Sonnet 0 · Haiku 0 · Opus 1 |", text)
        self.assertIn("| CLI 사용량(codex) | 입력 1 · 출력 2 · 캐시 읽기 3 |", text)


class CompareTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.runs = Path(self._td.name)

    def metrics(self, agreement, opus, fallback=None, cli_jobs=10, regressions=None):
        m = score.compute(self.runs, {})
        m.update(agreement=agreement, opus_calls=opus, fallback=fallback or {}, cli_jobs=cli_jobs)
        if regressions is not None:
            m.update(regression_measured=True, regressions=regressions)
        return m

    def verdict(self, ext, base):
        return score.compare_verdict_line(ext, base)

    def test_compare_verdict_line_passes_within_criteria(self):
        line = self.verdict(self.metrics(0.86, 3, {"한도": 1}), self.metrics(0.90, 3))
        self.assertIn("통과", line)
        self.assertNotIn("미달", line)
        self.assertIn("일치율 차이 4.0%p", line)
        self.assertIn("대체 실행 1/10", line)
        self.assertIn("회귀 미측정", line)

    def test_compare_verdict_line_fails_on_agreement(self):
        line = self.verdict(self.metrics(0.84, 3, {"한도": 1}), self.metrics(0.90, 3))
        self.assertIn("미달(일치율)", line)

    def test_compare_agreement_boundary_is_inclusive(self):
        self.assertIn("통과", self.verdict(self.metrics(0.85, 3), self.metrics(0.90, 3)))

    def test_compare_fails_on_opus_fallback_and_regression_with_all_causes(self):
        line = self.verdict(self.metrics(0.90, 4, {"오류": 2}, regressions=1), self.metrics(0.90, 3))
        self.assertIn("미달(Opus 호출, 대체 실행, 회귀)", line)

    def test_compare_fallback_boundary_is_ten_percent(self):
        self.assertIn("통과", self.verdict(self.metrics(0.90, 3, {"한도": 1}), self.metrics(0.90, 3)))
        self.assertIn("미달(대체 실행)", self.verdict(self.metrics(0.90, 3, {"한도": 2}), self.metrics(0.90, 3)))

    def test_compare_without_cli_jobs_is_not_a_pass(self):
        line = self.verdict(self.metrics(0.90, 3, cli_jobs=0), self.metrics(0.90, 3))
        self.assertIn("미달(대체 실행 미측정)", line)

    def test_compare_without_agreement_is_not_a_pass(self):
        self.assertIn("미달(일치율)", self.verdict(self.metrics(None, 3), self.metrics(0.90, 3)))

    def test_compare_table_puts_both_runs_side_by_side(self):
        ext, base = self.metrics(0.86, 3, {"한도": 1}), self.metrics(0.90, 3, cli_jobs=0)
        ext["claude_calls"], base["claude_calls"] = {"sonnet": 0, "haiku": 1, "opus": 3}, {"sonnet": 4, "haiku": 4, "opus": 3}
        text = score.render_compare(ext, base)
        self.assertIn("| 값 | Claude 작업자 | 외부 작업자 |", text)
        self.assertIn("| Claude 호출 | Sonnet 4 · Haiku 4 · Opus 3 | Sonnet 0 · Haiku 1 · Opus 3 |", text)
        self.assertIn("| Opus 호출 수 | 3 | 3 |", text)
        self.assertIn("| 대체 실행 | - | 1/10 (10.0%) — 한도 1 |", text)
        self.assertIn("통과", text.splitlines()[-1])

    def test_compare_cli_prints_table_and_verdict(self):
        sample = FIXTURES / "sample.jsonl"
        base, ext = self.runs / "claude", self.runs / "external"
        make_run(base, "s1", [decision("합의-사실", "a")])
        make_run(ext, "s1", [decision("합의-사실", "a")],
                 events=[workers_event("stage1", {"ok": 10})])
        out = self.runs / "cmp.md"
        p = subprocess.run([sys.executable, str(ROOT / "eval" / "score.py"), "--runs", str(ext), "--baseline-runs", str(base),
                            "--sample", str(sample), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("| 값 | Claude 작업자 | 외부 작업자 |", p.stdout)
        self.assertIn("비교 판정: 통과", p.stdout)
        self.assertNotIn("성공 기준:", p.stdout)
        self.assertIn("비교 판정: 통과", out.read_text(encoding="utf-8"))


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

    def test_replay_fake_skips_adjudicate_without_adj_args(self):
        sample = self.write_sample([self.item("item-01")])
        fake = self.write_fake("item-01")
        write_json(fake / "item-01" / "adj.json", {"results": [
            {"id": "r1-01:adjudicator", "output": {"verdict": "사실", "needs_design_decision": False, "reason": "x",
                                                   "verify": [], "model_id": "claude-opus-5-5"}}]})
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake)), 0)
        base = self.out / "item-01"
        self.assertNotIn("error", json.loads((base / "meta.json").read_text(encoding="utf-8")))
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        self.assertFalse((base / "run" / "round-1" / "adj.json").exists())

    def test_replay_passes_adopt_on_both_pass_to_init(self):
        sample = self.write_sample([self.item("item-01")])
        fake = self.write_fake("item-01")
        self.assertEqual(self.run_replay(sample, "--fake-results", str(fake), "--adopt-on-both-pass"), 0)
        ledger = json.loads((self.out / "item-01" / "ledger.json").read_text(encoding="utf-8"))
        self.assertIs(ledger["adopt_on_both_pass"], True)

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


class ReplayExternalTest(WorkersTestBase):
    def setUp(self):
        super().setUp()
        self.use_fakes()
        self.env["FAKE_CODEX_RESULT"] = result_json(verdict="아님")
        self.env["FAKE_CURSOR_RESULTS"] = json.dumps([self.sandbox_reply, {"result": fenced(result_json(verdict="아님"))}],
                                                     ensure_ascii=False)
        self.out = self.tmp / "out"
        self.fake = self.tmp / "fake"

    def sample(self):
        item = {"id": "item-01", "repo": str(self.repo), "commit": git(self.repo, "rev-parse", "HEAD"),
                "source": "codex-code", "doc": None, "finding": FINDING,
                "truth": {"verdict": "아님", "accepted_patch": None}, "baseline_opus_calls": 1}
        path = self.tmp / "sample.jsonl"
        path.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def replay(self, *extra):
        argv = ["--sample", str(self.sample()), "--out", str(self.out), "--repos-toml", self.env["CASCADE_REPOS_TOML"], *extra]
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()) as buf:
            code = replay.main(argv)
        return code, buf.getvalue()

    def pending(self):
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        return Path(meta["pending_run_dir"])

    def finish_by_cli(self):
        base = self.out / "item-01"
        env = {**self.env, "CASCADE_REPOS_TOML": str(base / "repos.toml")}
        run_dir = str(self.pending())
        for command in ("judge", "apply", "report"):
            extra = ["--round", "1"] if command != "report" else []
            p = run_cli([command, "--run-dir", run_dir, *extra], cwd=base / "wt", env=env)
            self.assertEqual(p.returncode, 0, f"{command}\n{p.stdout}\n{p.stderr}")

    def test_replay_external_runs_workers_with_fake_cli_then_collects(self):
        code, _ = self.replay("--workers", "external")
        self.assertEqual(code, 0)
        run_dir = self.pending()
        self.assertTrue((run_dir / "round-1" / "stage1-cli.json").is_file())
        self.assertFalse((run_dir / "round-1" / "stage1.json").exists())
        self.finish_by_cli()
        code, _ = self.replay("--collect")
        self.assertEqual(code, 0)
        base = self.out / "item-01"
        ledger = json.loads((base / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["workers_choice"], "external")
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["outcome"], "합의-아님")
        self.assertTrue((base / "run" / "report.md").is_file())
        m = score.compute(self.out, score.load_sample(self.sample()))
        self.assertEqual((m["agree"], m["judged"], m["cli_jobs"], m["fallback"]), (1, 1, 2, {}))
        self.assertEqual(m["claude_calls"], {"sonnet": 0, "haiku": 0, "opus": 0})
        self.assertEqual(set(m["usage"]), {"codex", "cursor"})

    def test_replay_external_uses_fallback_results_for_failed_cli_job(self):
        self.cursor_results([dict(LIMIT_ENTRY, match="r1-01-b")])
        code, _ = self.replay("--workers", "external")
        self.assertEqual(code, 0)
        write_json(self.pending() / "round-1" / "stage1-fallback.json", {"results": [
            {"id": "r1-01:b", "output": {"verdict": "아님", "needs_design_decision": False, "reason": "쓰임이 없다",
                                         "verify": [], "model_id": "claude-haiku-5-5"}}]})
        self.finish_by_cli()
        code, _ = self.replay("--collect")
        self.assertEqual(code, 0)
        m = score.compute(self.out, score.load_sample(self.sample()))
        self.assertEqual((m["agree"], m["judged"]), (1, 1))
        self.assertEqual((m["fallback"], m["claude_calls"]["haiku"]), ({"한도": 1}, 1))

    def test_replay_rejects_fake_results_with_external_workers_before_any_cli_call(self):
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as ctx:
                replay.main(["--sample", str(self.sample()), "--out", str(self.out), "--workers", "external",
                             "--fake-results", str(self.fake)])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("external", err.getvalue())
        self.assertIn("모델을 부르지 않", err.getvalue())
        self.assertFalse((self.out / "item-01").exists())

    def test_replay_external_without_fake_results_stops_after_workers(self):
        code, text = self.replay("--workers", "external")
        self.assertEqual(code, 0)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        run_dir = Path(meta["pending_run_dir"])
        self.assertTrue((run_dir / "round-1" / "stage1-cli.json").is_file())
        self.assertFalse((run_dir / "round-1" / "decisions.json").exists())
        self.assertIn("workers", text)

    def test_replay_claude_workers_is_default(self):
        code, _ = self.replay()
        self.assertEqual(code, 0)
        meta = json.loads((self.out / "item-01" / "meta.json").read_text(encoding="utf-8"))
        ledger = json.loads((Path(meta["pending_run_dir"]) / "ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["workers_choice"], "claude")
        self.assertFalse((Path(meta["pending_run_dir"]) / "round-1" / "stage1-cli.json").exists())


if __name__ == "__main__":
    unittest.main()
