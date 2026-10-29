import json
import os
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli, cli_steps, report  # noqa: E402
from cascade.cli_steps import claude_job_entries  # noqa: E402
from cascade.runners import Outcome  # noqa: E402
from fake_cli import CLAUDE_BODY, install, result_json  # noqa: E402
from test_cli_e2e import CALC, FIXTURES, CliTestBase, fix  # noqa: E402

INPUT = str(FIXTURES / "codex-code-e2e.final")
CODE_FIXES = {
    "r1-01-": {"calc.py": fix(CALC, "return a - b", "return a + b")},
    "r1-02-": {"calc.py": fix(CALC, "return a + b", "return a * b")},
}


class ClaudeViaTestBase(CliTestBase):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.claude = install(bin_dir, "claude", CLAUDE_BODY)
        self.env.update({"CASCADE_REVIEWER": "claude", "CASCADE_CLAUDE": str(self.claude), "FAKE_CLI_LOG": str(self.tmp / "log"),
                         "FAKE_CLAUDE_RESULT": result_json(), "FAKE_MUTATE_WT": json.dumps(CODE_FIXES, ensure_ascii=False)})

    def rd(self):
        return self.run_dir / "round-1"

    def read(self, name):
        return json.loads((self.rd() / name).read_text(encoding="utf-8"))

    def start(self, *extra):
        p = self.cli("start", "--source", "codex-code", "--input", INPUT, *extra)
        self.run_dir = Path(next(x for x in p.stdout.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        return p

    def calls(self):
        return len(list((self.tmp / "log").glob("claude-*.args.json")))


class ClaudeViaJobsTest(ClaudeViaTestBase):
    def test_default_keeps_workflow_files(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual(self.ledger().claude_via, "workflow")
        self.assertEqual(len(self.read("stage1-args.json")["jobs"]), 4)
        self.assertFalse((self.rd() / "stage1-cli-args.json").exists())

    def test_cli_sends_claude_jobs_to_cli_file_with_metadata(self):
        self.init("--claude-via", "cli")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertEqual(self.ledger().claude_via, "cli")
        self.assertEqual(self.read("stage1-args.json"), {"jobs": []})
        jobs = self.read("stage1-cli-args.json")["jobs"]
        self.assertEqual([(j["id"], j["runner"], j["model"], j["writable"]) for j in jobs],
                         [("r1-01:a", "claude", "claude-sonnet-5-5", True), ("r1-01:b", "claude", "claude-haiku-5-5", True),
                          ("r1-02:a", "claude", "claude-sonnet-5-5", True), ("r1-02:b", "claude", "claude-haiku-5-5", True)])
        prompt = Path(jobs[0]["promptPath"]).read_text(encoding="utf-8")
        self.assertIn("커밋하지 않는다", prompt)

    def test_jobs_force_can_switch_path_before_judge(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", "--force", "--claude-via", "cli")
        self.assertEqual(self.read("stage1-args.json"), {"jobs": []})
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", "--force", "--claude-via", "workflow")
        self.assertEqual(len(self.read("stage1-args.json")["jobs"]), 4)
        self.assertFalse((self.rd() / "stage1-cli-args.json").exists())


class JobsLockTest(ClaudeViaTestBase):
    def test_jobs_is_refused_while_workers_holds_the_lock(self):
        self.init("--claude-via", "cli")
        with cli_steps.workers_lock(self.run_dir):
            p = self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("workers가 이미 돈다", p.stdout + p.stderr)
        self.assertFalse((self.rd() / "stage1-cli-args.json").exists())


class ClaudeViaGoTest(ClaudeViaTestBase):
    def test_start_runs_workers_without_workflow_and_stops_only_for_review_workflow(self):
        p = self.start("--claude-via", "cli")
        before_stop = p.stdout.split("멈춤:")[0]
        self.assertNotIn("stage1-args.json 내용", before_stop)
        self.assertIn("멈춤: Workflow 결과가 필요하다", p.stdout)
        self.assertIn("recheck-args.json", p.stdout)
        self.assertEqual(self.calls(), 4)
        outcomes = {d["finding_id"]: d["outcome"] for d in self.ledger().rounds[0].decisions}
        self.assertEqual(outcomes, {"r1-01": "합의-사실", "r1-02": "합의-사실"})

    def test_workers_records_claude_usage_per_job_for_report(self):
        self.start("--claude-via", "cli")
        data = self.read(cli_steps.USAGE_FILE)
        rows = next(iter(data["workflows"].values()))
        self.assertEqual(sorted(r["job_id"] for r in rows), ["r1-01:a", "r1-01:b", "r1-02:a", "r1-02:b"])
        self.assertEqual({r["cell"] for r in rows}, {"code.a", "code.b"})
        self.assertTrue(all(r["cost"] == 0.25 and r["input"] == 100 for r in rows))
        self.assertIn("prices_as_of", data)

    def test_second_record_keeps_earlier_attempt_rows(self):
        self.start("--claude-via", "cli")
        entries = self.read("stage1-cli-args.json")["jobs"]
        outcome = Outcome("r1-01:a", "claude", "ok", None, {"input_tokens": 1, "cost_usd": 0.5}, "")
        cli._record_claude_usage(self.rd(), "code", entries, [outcome], "stage1")
        workflows = self.read(cli_steps.USAGE_FILE)["workflows"]
        self.assertEqual(sorted(workflows), ["cli-stage1-1", "cli-stage1-2"])
        self.assertEqual(len(workflows["cli-stage1-1"]), 4)

    def test_attempt_without_envelope_is_recorded_as_unmeasured_row(self):
        self.start("--claude-via", "cli")
        entries = self.read("stage1-cli-args.json")["jobs"]
        lost = Outcome("r1-01:a", "claude", "timeout", None, {"unrecorded": 1}, "")
        skipped = Outcome("r1-01:b", "claude", "skipped", None, {}, "")
        cli._record_claude_usage(self.rd(), "code", entries, [lost, skipped], "stage1")
        rows = self.read(cli_steps.USAGE_FILE)["workflows"]["cli-stage1-2"]
        self.assertEqual([(r["job_id"], r["cost"]) for r in rows], [("r1-01:a", None)])

    def test_report_expects_claude_cli_jobs_so_missing_usage_is_unmeasured(self):
        self.start("--claude-via", "cli")
        (self.rd() / cli_steps.USAGE_FILE).unlink()
        usage = report._ClaudeUsage(self.ledger(), self.run_dir)
        self.assertEqual(dict(usage.jobs["code.a"]), {(1, "r1-01:a", "cli"): 1, (1, "r1-02:a", "cli"): 1})
        self.assertEqual(usage.columns([], usage.jobs["code.a"], True)[0], "미측정")

    def test_failed_claude_job_goes_to_workflow_fallback_and_keeps_failed_cost(self):
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "boom"})
        self.init("--claude-via", "cli")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.cli("workers", "--run-dir", str(self.run_dir), "--round", "1", "--stage", "stage1")
        fallback = self.read("stage1-fallback-args.json")["jobs"]
        self.assertEqual(len(fallback), 4)
        rows = next(iter(self.read(cli_steps.USAGE_FILE)["workflows"].values()))
        self.assertEqual(len(rows), 4)

    def test_workflow_fallback_run_of_failed_cli_job_shows_as_unmeasured(self):
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "boom"})
        self.init("--claude-via", "cli")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.cli("workers", "--run-dir", str(self.run_dir), "--round", "1", "--stage", "stage1")
        usage = report._ClaudeUsage(self.ledger(), self.run_dir)
        tokens, cost = usage.columns(usage.rows["code.a"], usage.jobs["code.a"], True)
        self.assertEqual(tokens, "200/40/10 외 미측정 2건")
        self.assertEqual(cost, "$0.5000 외 미측정 2건")
        self.assertEqual(usage.unmeasured, 2)

    def test_attempt_without_envelope_shows_tokens_as_unmeasured(self):
        self.start("--claude-via", "cli")
        entries = self.read("stage1-cli-args.json")["jobs"]
        lost = Outcome("r1-01:a", "claude", "timeout", None, {"unrecorded": 1}, "")
        cli._record_claude_usage(self.rd(), "code", entries, [lost], "stage1")
        usage = report._ClaudeUsage(self.ledger(), self.run_dir)
        rows = [r for r in usage.rows["code.a"] if r["job_id"] == "r1-01:a"][-1:]
        self.assertEqual(usage.columns(rows, Counter(), True), ("미측정", "미측정"))

    def test_report_points_to_login_when_claude_cli_auth_fails(self):
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"})
        self.init("--claude-via", "cli")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        self.cli("workers", "--run-dir", str(self.run_dir), "--round", "1", "--stage", "stage1")
        text = self.cli("report", "--run-dir", str(self.run_dir)).stdout
        self.assertIn("claude: 로그인이 풀렸다. `claude /login`", text)

    def test_eval_scorer_expects_claude_cli_jobs_but_not_external_ones(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
        import score
        self.start("--claude-via", "cli")
        expected = {"r1-01:a", "r1-01:b", "r1-02:a", "r1-02:b"}
        self.assertEqual(score.claude_jobs(self.rd(), "code", score.COST_CORE), expected)
        cli_args = self.read("stage1-cli-args.json")
        cli_args["jobs"][0]["runner"] = "codex"
        (self.rd() / "stage1-cli-args.json").write_text(json.dumps(cli_args), encoding="utf-8")
        self.assertEqual(score.claude_jobs(self.rd(), "code", score.COST_CORE), expected - {"r1-01:a"})

    def test_eval_scorer_counts_missing_workflow_fallback_usage_as_gap(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
        import score
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "boom"})
        self.init("--claude-via", "cli")
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for _ in range(2):
            self.cli("workers", "--run-dir", str(self.run_dir), "--round", "1", "--stage", "stage1", "--force")
        _, _, tags, gaps = score.collect_usage(json.loads((self.run_dir / "ledger.json").read_text(encoding="utf-8")), [self.rd()], score.COST_CORE)
        self.assertEqual(tags, [""])
        self.assertEqual(gaps, {"code.a", "code.b"})

    def test_claude_job_entries_counts_claude_cli_and_workflow_jobs_only(self):
        data = {"jobs": [{"id": "a", "runner": "claude"}, {"id": "b", "runner": "codex"}, {"id": "c"}]}
        self.assertEqual([j["id"] for j in claude_job_entries(data)], ["a", "c"])
        self.assertEqual(claude_job_entries(None), [])


if __name__ == "__main__":
    unittest.main()
