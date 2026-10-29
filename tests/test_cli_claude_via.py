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
from cascade.rounds import RoundRecord  # noqa: E402
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


class ClaudeViaAdjudicateTest(ClaudeViaTestBase):
    def stage(self, command, *extra, ok=True):
        return self.cli(command, "--run-dir", str(self.run_dir), "--round", "1", *extra, ok=ok)

    def prepare_adj(self, via="cli"):
        self.init("--claude-via", via)
        self.stage("jobs")
        if via == "cli":
            self.stage("workers", "--stage", "stage1")
            data = self.read("stage1-cli.json")
        else:
            data = {"results": [{"id": j["id"], "output": json.loads(result_json(model_id=j["model"]))}
                                for j in self.read("stage1-args.json")["jobs"]]}
        for item in data["results"]:
            if item["id"].endswith(":b"):
                item["output"]["verdict"] = "아님"
        name = "stage1-cli.json" if via == "cli" else "stage1.json"
        cli_steps.write_json(self.rd() / name, data)
        self.stage("judge", *(["--results", str(self.rd() / name)] if via == "workflow" else []))
        self.env["FAKE_MUTATE_WT"] = "{}"

    def prepare_opinion(self):
        self.prepare_adj()
        self.env["FAKE_CLAUDE_RESULT"] = result_json(verdict="못 정함")
        self.stage("workers", "--stage", "adj")
        self.stage("adjudicate")
        self.env["FAKE_CLAUDE_RESULT"] = json.dumps({"text": "opinion text", "model_id": "reported"})

    def actions(self):
        return cli_steps.next_actions(self.ledger(), self.run_dir)

    def test_judge_force_clears_adj_cli_files_when_workers_agree(self):
        self.prepare_adj()
        self.assertIn("--stage adj", self.actions()[0].text)
        stale = ("adj-args.json", "adj-cli-args.json", "adj-cli.json", "adj-fallback-args.json", "adj-fallback.json")
        for name in stale[2:]:
            cli_steps.write_json(self.rd() / name, {"jobs": []})
        data = self.read("stage1-cli.json")
        for item in data["results"]:
            item["output"]["verdict"] = "아님"
        cli_steps.write_json(self.rd() / "stage1-cli.json", data)
        self.stage("judge", "--force")
        self.assertEqual({d["outcome"] for d in self.read("decisions.json")}, {"합의-아님"})
        self.assertFalse(any("--stage adj" in a.text for a in self.actions()))
        for name in stale:
            with self.subTest(name=name):
                self.assertFalse((self.rd() / name).exists())
        self.assertEqual([a.argv[0] for a in self.actions()], ["apply"])

    def test_judge_force_keeps_workflow_adj_file_when_workers_agree(self):
        self.prepare_adj("workflow")
        before = (self.rd() / "adj-args.json").read_bytes()
        data = self.read("stage1.json")
        for item in data["results"]:
            item["output"]["verdict"] = "아님"
        cli_steps.write_json(self.rd() / "stage1.json", data)
        self.stage("judge", "--results", str(self.rd() / "stage1.json"), "--force")
        self.assertEqual({d["outcome"] for d in self.read("decisions.json")}, {"합의-아님"})
        self.assertEqual((self.rd() / "adj-args.json").read_bytes(), before)
        self.assertEqual([a.kind for a in self.actions()], ["workflow", "cmd"])

    def test_default_adj_and_opinion_keep_workflow_files_and_steps(self):
        self.prepare_adj("workflow")
        self.assertFalse((self.rd() / "adj-cli-args.json").exists())
        text = report.render(self.ledger(), dry_run=True, run_dir=self.run_dir)
        self.assertIn("| code.adjudicator | 2 |", text)
        self.assertEqual([a.kind for a in self.actions()], ["workflow", "cmd"])
        self.assertEqual(self.actions()[0].args_path, self.rd() / "adj-args.json")
        for job in self.read("adj-args.json")["jobs"]:
            prompt = Path(job["promptPath"]).read_text()
            self.assertIn("고쳤으면(1·2) 위 검증 명령을 워크트리에서 직접 돌리고 돌린 명령과 통과 여부를 `verify`에 적은 뒤 규칙의 커밋 명령으로 커밋한다.", prompt)
            self.assertNotIn("{commit_step}", prompt)
        data = {"results": [{"id": j["id"], "output": json.loads(result_json(verdict="못 정함", model_id=j["model"]))}
                            for j in self.read("adj-args.json")["jobs"]]}
        cli_steps.write_json(self.rd() / "adj.json", data)
        self.stage("adjudicate", "--results", str(self.rd() / "adj.json"))
        self.assertFalse((self.rd() / "opinion-cli-args.json").exists())
        self.assertTrue((self.rd() / "opinion-args.json").is_file())
        text = report.render(self.ledger(), dry_run=True, run_dir=self.run_dir)
        self.assertIn("| Claude 의견 | 0 |", text)
        self.assertEqual([a.kind for a in self.actions()], ["workflow", "cmd"])
        self.assertIn("opinion.json", self.actions()[1].text)

    def test_adj_cli_metadata_workers_and_adjudicate_without_workflow(self):
        self.prepare_adj()
        jobs = self.read("adj-cli-args.json")["jobs"]
        cell = cli.assignment.adjudicator_cell(self.ledger())
        self.assertFalse(cli_steps.has_jobs(self.rd() / "adj-args.json"))
        for j in jobs:
            self.assertEqual((j["role"], j["kind"], j["runner"], j["writable"]),
                             ("adjudicator", "adjudicator", "claude", True))
            self.assertEqual((j["model"], j["effort"]), (cell["model"], cell["effort"]))
            self.assertEqual(Path(j["worktree"]), self.wt(j["id"].replace(":adjudicator", "-adj")))
            self.assertIn("커밋하지 않는다", Path(j["promptPath"]).read_text())
            self.assertNotIn("규칙의 커밋 명령으로 커밋한다", Path(j["promptPath"]).read_text())
        self.assertTrue(self.actions()[0].background)
        self.assertIn("--stage adj", self.actions()[0].text)
        p = self.stage("adjudicate", ok=False)
        self.assertIn("--stage adj", p.stdout + p.stderr)
        self.env["FAKE_CLAUDE_RESULT"] = result_json(verdict="아님")
        self.stage("workers", "--stage", "adj")
        text = report.render(self.ledger(), dry_run=True, run_dir=self.run_dir)
        self.assertIn(f"| code.adjudicator | {len(jobs)} |", text)
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
        import score
        cells = {}
        score.add_cell_calls(cells, json.loads((self.run_dir / "ledger.json").read_text()), [self.rd()], score.COST_CORE)
        self.assertEqual(cells["code.adjudicator"]["calls"], len(jobs))
        self.assertEqual([a.argv[0] for a in self.actions()], ["adjudicate"])
        p = self.stage("adjudicate")
        self.assertNotIn("모델 ID", p.stdout)
        self.assertTrue(cli_steps.step_done(self.ledger().rounds[0], "adjudicate"))
        result = self.read("results.json")["r1-01:adjudicator"]
        self.assertEqual((result["verdict"], result["model_id"]), ("아님", "claude-opus-5-5"))
        decision = next(d for d in self.read("decisions.json") if d["finding_id"] == "r1-01")
        self.assertEqual((decision["outcome"], decision["patch_from"]), ("판가름", None))
        self.assertIn("판가름: 아님", decision["reasons"])
        self.assertNotEqual(self.stage("workers", "--stage", "adj", "--force", ok=False).returncode, 0)

    def test_adj_failure_fallback_merge_and_usage(self):
        self.prepare_adj()
        ledger = self.ledger()
        ledger.profile["roles"]["code.adjudicator"]["fallback"] = {
            "runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"}
        cli_steps.save_ledger(self.run_dir, ledger)
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "boom"})
        self.stage("workers", "--stage", "adj")
        jobs = self.read("adj-fallback-args.json")["jobs"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual({j["model"] for j in jobs}, {"claude-sonnet-5-5"})
        rows = self.read(cli_steps.USAGE_FILE)["workflows"]["cli-adj-1"]
        self.assertEqual({r["cell"] for r in rows}, {"code.adjudicator"})
        self.assertTrue(all(r["cost"] == 0.25 for r in rows))
        p = self.stage("adjudicate", ok=False)
        self.assertIn("adj-fallback.json", p.stdout + p.stderr)
        self.assertEqual([a.kind for a in self.actions()], ["workflow", "cmd"])
        cli_steps.write_json(self.rd() / "adj-fallback.json", {"results": [
            {"id": j["id"], "output": json.loads(result_json(verdict="아님", model_id=j["model"]))} for j in jobs]})
        self.assertNotIn("모델 ID", self.stage("adjudicate").stdout)
        results = self.read("results.json")
        for job in jobs:
            self.assertEqual((results[job["id"]]["verdict"], results[job["id"]]["model_id"]),
                             ("아님", job["model"]))
        self.assertTrue(all("판가름: 아님" in d["reasons"] for d in self.read("decisions.json")))

    def test_adj_force_restores_first_input_before_rerun(self):
        self.prepare_adj()
        base = (self.wt("r1-01-adj") / "calc.py").read_text()
        self.env["FAKE_MUTATE_WT"] = json.dumps({"r1-01-adj": {"calc.py": "changed"}})
        self.stage("workers", "--stage", "adj")
        self.assertEqual((self.wt("r1-01-adj") / "calc.py").read_text(), "changed")
        self.assertNotEqual(self.stage("workers", "--stage", "adj", ok=False).returncode, 0)
        self.env["FAKE_MUTATE_WT"] = "{}"
        self.stage("workers", "--stage", "adj", "--force")
        self.assertEqual((self.wt("r1-01-adj") / "calc.py").read_text(), base)
        self.assertEqual(sorted(k for k in self.read(cli_steps.USAGE_FILE)["workflows"] if k.startswith("cli-adj-")),
                         ["cli-adj-1", "cli-adj-2"])

    def test_opinion_cli_metadata_and_ledger(self):
        self.prepare_opinion()
        result = self.read("results.json")["r1-01:adjudicator"]
        self.assertEqual((result["verdict"], result["model_id"]), ("못 정함", "claude-opus-5-5"))
        self.assertEqual({d["outcome"] for d in self.read("decisions.json")}, {"판단불가"})
        self.assertFalse((self.rd() / "opinion-args.json").exists())
        cell = cli_steps.review_cell(self.ledger(), "opinion")
        for j in self.read("opinion-cli-args.json")["jobs"]:
            self.assertEqual((j["model"], j["effort"]), (cell["model"], cell["effort"]))
            self.assertEqual((j["role"], j["kind"], j["runner"], j["writable"]), ("opinion", "review", "claude", False))
            self.assertEqual(Path(j["worktree"]), self.wt("base"))
            self.assertIn(str(self.wt("base")), Path(j["promptPath"]).read_text())
        self.assertTrue(self.actions()[0].background)
        self.stage("workers", "--stage", "opinion")
        text = report.render(self.ledger(), dry_run=True, run_dir=self.run_dir)
        self.assertIn("| Claude 의견 | 0 |", text)
        self.assertEqual([a.argv[0] for a in self.actions()], ["opinion"])
        self.stage("opinion", "--results", str(self.rd() / "opinion-cli.json"))
        text = report.render(self.ledger(), dry_run=True, run_dir=self.run_dir)
        self.assertIn("| Claude 의견 | 2 |", text)
        self.assertEqual(self.ledger().opinions, {"r1-01": "opinion text", "r1-02": "opinion text"})
        self.assertNotEqual(self.stage("workers", "--stage", "opinion", "--force", ok=False).returncode, 0)
        rows = self.read(cli_steps.USAGE_FILE)["workflows"]["cli-opinion-1"]
        self.assertEqual({r["cell"] for r in rows}, {"code.opinion"})

    def test_failed_opinion_has_no_workflow_fallback_and_records_note(self):
        self.prepare_opinion()
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "boom"})
        p = self.stage("workers", "--stage", "opinion")
        self.assertNotIn("Claude 대체", p.stdout)
        event = next(e for e in self.ledger().rounds[0].events if e["step"] == "workers-opinion")
        self.assertEqual(event["fallback"], [])
        self.assertEqual(event["failed"], ["r1-01:opinion", "r1-02:opinion"])
        self.assertFalse((self.rd() / "opinion-fallback-args.json").exists())
        self.stage("opinion", "--results", str(self.rd() / "opinion-cli.json"))
        self.assertEqual(set(self.ledger().opinions.values()), {cli.CALL_FAILED_NOTE})
        text = report.render(self.ledger(), dry_run=True, run_dir=self.run_dir)
        line = next(line for line in text.splitlines() if "회차 1 opinion:" in line)
        self.assertNotIn("대체 실행", line)
        self.assertIn("의견 없음 2건", line)
        reasons, tokens = report._worker_fallbacks(self.ledger(), self.run_dir)
        self.assertNotIn("code.opinion", reasons)
        self.assertIn("code.opinion", tokens)

    def test_opinion_force_calls_claude_after_its_own_auth_recovers(self):
        self.prepare_opinion()
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"})
        self.stage("workers", "--stage", "opinion")
        self.assertIn("auth", {o["status"] for o in self.read("opinion-cli.json")["outcomes"]})
        self.env.pop("FAKE_CLAUDE_ENVELOPE")
        before = self.calls()
        self.stage("workers", "--stage", "opinion", "--force")
        self.assertEqual(self.calls() - before, 2)
        self.assertEqual({o["status"] for o in self.read("opinion-cli.json")["outcomes"]}, {"ok"})
        self.stage("opinion", "--results", str(self.rd() / "opinion-cli.json"))
        self.assertEqual(set(self.ledger().opinions.values()), {"opinion text"})

    def test_opinion_calls_claude_after_adjudicator_auth_recovers(self):
        self.prepare_adj()
        self.env["FAKE_CLAUDE_ENVELOPE"] = json.dumps({"is_error": True, "result": "Invalid API key · Please run /login"})
        self.stage("workers", "--stage", "adj")
        skipped_output = self.stage("workers", "--stage", "adj", "--force").stdout
        self.env.pop("FAKE_CLAUDE_ENVELOPE")
        cli_steps.write_json(self.rd() / "adj-fallback.json", {"results": [
            {"id": j["id"], "output": json.loads(result_json(verdict="못 정함", model_id=j["model"]))}
            for j in self.read("adj-fallback-args.json")["jobs"]]})
        self.stage("adjudicate")
        self.env["FAKE_CLAUDE_RESULT"] = json.dumps({"text": "recovered opinion", "model_id": "reported"})
        before = self.calls()
        p = self.stage("workers", "--stage", "opinion")
        self.assertEqual(self.calls() - before, 2)
        self.assertIn("건너뜀", skipped_output)
        self.assertIn("run /login", skipped_output)
        self.assertEqual({o["status"] for o in self.read("opinion-cli.json")["outcomes"]}, {"ok"})
        self.assertNotIn("건너뜀", p.stdout)
        self.stage("opinion", "--results", str(self.rd() / "opinion-cli.json"))
        self.assertEqual(set(self.ledger().opinions.values()), {"recovered opinion"})

    def test_readonly_opinion_mutation_discards_output_and_restores_base(self):
        self.prepare_opinion()
        self.env["FAKE_MUTATE_WT"] = json.dumps({"base": {"calc.py": "changed"}})
        before = (self.wt("base") / "calc.py").read_text()
        self.stage("workers", "--stage", "opinion")
        self.assertEqual({o["status"] for o in self.read("opinion-cli.json")["outcomes"]}, {"invalid"})
        self.assertEqual((self.wt("base") / "calc.py").read_text(), before)
        self.stage("opinion", "--results", str(self.rd() / "opinion-cli.json"))
        self.assertEqual(set(self.ledger().opinions.values()), {cli.CALL_FAILED_NOTE})

    def test_opinions_sharing_base_run_without_overlap(self):
        self.prepare_opinion()
        self.env["FAKE_CLAUDE_SLEEP"] = "0.3"
        before = self.calls()
        self.stage("workers", "--stage", "opinion")
        first_end = float((self.tmp / "log" / f"claude-{before + 1}.end").read_text())
        second_start = float((self.tmp / "log" / f"claude-{before + 2}.start").read_text())
        self.assertGreaterEqual(second_start, first_end)

    def test_missing_base_keeps_opinion_workflow(self):
        self.prepare_adj()
        self.env["FAKE_CLAUDE_RESULT"] = result_json(verdict="못 정함")
        self.stage("workers", "--stage", "adj")
        ledger = self.ledger()
        ledger.snapshot["worktrees"].pop("base")
        cli_steps.save_ledger(self.run_dir, ledger)
        self.stage("adjudicate")
        self.assertFalse((self.rd() / "opinion-cli-args.json").exists())
        self.assertTrue((self.rd() / "opinion-args.json").is_file())

    def test_jobs_force_clears_adj_opinion_results_and_events(self):
        self.prepare_opinion()
        self.stage("workers", "--stage", "opinion")
        for name in ("adj-fallback-args.json", "adj-fallback.json"):
            cli_steps.write_json(self.rd() / name, {"jobs": []})
        self.stage("jobs", "--force")
        for pattern in ("adj*.json", "opinion*.json"):
            self.assertEqual(list(self.rd().glob(pattern)), [])
        events = self.ledger().rounds[0].events
        self.assertFalse(any(e["step"] in ("workers-adj", "workers-opinion") for e in events))
        self.stage("judge", ok=False)
        self.assertFalse(cli_steps.workers_done(self.ledger().rounds[0], "adj"))

    def test_report_and_eval_include_unmeasured_adj_and_opinion(self):
        self.prepare_opinion()
        (self.rd() / cli_steps.USAGE_FILE).unlink()
        usage = report._ClaudeUsage(self.ledger(), self.run_dir)
        for cell in ("code.adjudicator", "code.opinion"):
            self.assertEqual(sum(usage.jobs[cell].values()), 2)
            self.assertEqual(usage.columns([], usage.jobs[cell], True)[0], "미측정")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))
        import score
        self.assertTrue({"r1-01:adjudicator", "r1-01:opinion"} <= score.claude_jobs(self.rd(), "code", score.COST_ALL))
        self.assertNotIn("r1-01:opinion", score.claude_jobs(self.rd(), "code", score.COST_CORE))
        _, _, _, gaps = score.collect_usage(json.loads((self.run_dir / "ledger.json").read_text()), [self.rd()], score.COST_ALL)
        self.assertTrue({"code.adjudicator", "code.opinion"} <= gaps)
        cells = {}
        score.add_cell_calls(cells, json.loads((self.run_dir / "ledger.json").read_text()), [self.rd()], score.COST_ALL)
        self.assertEqual(cells["code.opinion"]["calls"], 2)

    def test_start_go_runs_adj_opinion_to_report_without_workflow(self):
        self.env["FAKE_MUTATE_WT"] = "{}"
        self.env["FAKE_CLAUDE_RESULTS_MODELS"] = json.dumps({
            "claude-haiku-5-5": json.loads(result_json(verdict="아님")),
            "claude-opus-5-5": json.loads(result_json(verdict="못 정함")),
            "claude-fable-5-1": {"text": "opinion text", "model_id": "reported"}})
        p = self.start("--claude-via", "cli")
        self.assertNotIn("Workflow 결과가 필요하다", p.stdout)
        self.assertTrue((self.run_dir / "report.md").is_file())
        self.assertEqual(self.calls(), 8)
        self.assertEqual(set(self.ledger().opinions.values()), {"opinion text"})
        for fid in ("r1-01", "r1-02"):
            result = self.read("results.json")[f"{fid}:adjudicator"]
            self.assertEqual(result["model_id"], "claude-opus-5-5")
            self.assertNotIn("호출 실패", result["reason"])


class ClaudeStageEventsTest(unittest.TestCase):
    def test_opinion_exhaustion_is_scoped_to_its_stage(self):
        rnd = RoundRecord(1, [], [], [], False)
        cli_steps.mark(rnd, "jobs")
        adj = {"claude": {"status": "auth", "reason": "adj login"}}
        opinion = {"claude": {"status": "limit", "reason": "opinion limit"}}
        cli_steps.mark(rnd, "workers-adj", exhausted=adj)
        self.assertEqual(cli_steps.exhausted_runners(rnd), adj)
        self.assertEqual(cli_steps.exhausted_runners(rnd, "opinion"), {})
        cli_steps.mark(rnd, "workers-opinion", exhausted=opinion)
        self.assertEqual(cli_steps.exhausted_runners(rnd, "opinion"), opinion)
        self.assertEqual(cli_steps.exhausted_runners(rnd), adj)

    def test_workers_use_each_stage_input_and_consumer_events(self):
        for stage, source, consumers in (("stage1", "jobs", ("judge",)), ("checker", "judge-checker", ("judge",)),
                                          ("adj", "judge", ("adjudicate",)),
                                          ("opinion", "adjudicate", ("opinion:r1-01", "apply"))):
            for consumer in consumers:
                with self.subTest(stage=stage, consumer=consumer):
                    rnd = RoundRecord(1, [], [], [], False)
                    cli_steps.mark(rnd, source)
                    self.assertFalse(cli_steps.workers_done(rnd, stage))
                    self.assertFalse(cli_steps.judged_after_workers_input(rnd, stage))
                    cli_steps.mark(rnd, f"workers-{stage}")
                    self.assertTrue(cli_steps.workers_done(rnd, stage))
                    cli_steps.mark(rnd, consumer)
                    self.assertTrue(cli_steps.judged_after_workers_input(rnd, stage))
                    cli_steps.mark(rnd, source)
                    self.assertFalse(cli_steps.workers_done(rnd, stage))
                    self.assertFalse(cli_steps.judged_after_workers_input(rnd, stage))


if __name__ == "__main__":
    unittest.main()
