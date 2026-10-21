import json
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import cli_steps, profiles  # noqa: E402
from cascade.rounds import Ledger  # noqa: E402
from cascade.snapshot import commit_worktree  # noqa: E402
from helpers import make_repo, run_cli, write_repos_toml  # noqa: E402
from test_claude_reviewer import review_results  # noqa: E402
from test_cli_e2e import CALC, RECHECK_RESULT, fix  # noqa: E402
from test_workers import ExternalWorkersBase  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NO_LEGACY_ENV = {"CASCADE_WORKERS": "", "CASCADE_REVIEWER": ""}
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-5-5"
OLD_STAGE1_JOBS = [
    {"id": "r1-01:a", "model": SONNET, "effort": "low"}, {"id": "r1-01:b", "model": HAIKU, "effort": "low"},
    {"id": "r1-02:a", "model": SONNET, "effort": "low"}, {"id": "r1-02:b", "model": HAIKU, "effort": "low"},
]


class CliProfilesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": "import unittest\n",
            ".gitignore": "__pycache__/\n",
        })
        toml = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["python3 -m unittest {files}"], "format": []}})
        self.env = {"CASCADE_REPOS_TOML": str(toml), "PYTHONDONTWRITEBYTECODE": "1", **NO_LEGACY_ENV}
        self.result = FIXTURES / "codex-code-e2e.final"

    def init(self, *extra, env=None):
        return run_cli(["init", "--source", "codex-code", "--input", str(self.result), *extra], self.repo,
                       env={**self.env, **(env or {})})

    def run_dir(self, p):
        line = next(x for x in p.stdout.splitlines() if x.startswith("run_dir="))
        return Path(line.split("=", 1)[1])

    def ledger(self, p):
        return json.loads((self.run_dir(p) / "ledger.json").read_text(encoding="utf-8"))

    def stage1_jobs(self, p):
        run_dir = self.run_dir(p)
        jobs = run_cli(["jobs", "--run-dir", str(run_dir), "--round", "1"], self.repo, env=self.env)
        self.assertEqual(jobs.returncode, 0, jobs.stdout + jobs.stderr)
        data = json.loads((run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        return [{k: j[k] for k in ("id", "model", "effort")} for j in data["jobs"]], data["jobs"]

    def old_behaviour(self, p, *, reviewer, review_model, review_effort):
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("폐기 예정", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("지적 2건", p.stdout)
        self.assertIn("프로필: default (builtin)", p.stdout)
        self.assertNotIn("덮음:", p.stdout)
        self.assertNotIn("대체:", p.stdout)
        ledger = self.ledger(p)
        self.assertEqual((ledger["workers_choice"], ledger["workers"], ledger["adopt_on_both_pass"]), ("claude", {}, False))
        self.assertEqual((ledger["reviewer"], ledger["review_model"], ledger["review_effort"]),
                         (reviewer, review_model, review_effort))
        self.assertEqual((ledger["profile"]["name"], ledger["profile"]["source"], ledger["profile"]["explicit"]),
                         ("default", "builtin", False))
        self.assertEqual((ledger["profile"]["overrides"], ledger["profile"]["substituted"]), ([], []))
        slim, full = self.stage1_jobs(p)
        self.assertEqual(slim, OLD_STAGE1_JOBS)
        self.assertEqual([j["kind"] for j in full], ["worker"] * 4)
        self.assertEqual(sorted(f.name for f in (self.run_dir(p) / "round-1").iterdir() if f.name.startswith("stage1")),
                         ["stage1-args.json"])
        self.assertEqual(sorted(f.name for f in (self.run_dir(p) / "round-1" / "prompts").iterdir()),
                         ["r1-01-a.md", "r1-01-b.md", "r1-02-a.md", "r1-02-b.md"])

    def test_init_without_profile_is_unchanged(self):
        script = self.tmp / "run-review.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        p = self.init(env={"CASCADE_RUN_REVIEW": str(script)})
        self.old_behaviour(p, reviewer="codex", review_model="", review_effort="")
        self.assertIn("역할: 첫 검토=Claude(Fable 5.1 high)", p.stdout)
        self.assertIn("의견=Codex, 재검토=Codex", p.stdout)
        self.assertIn("전송 동의: 의견·재검토 → OpenAI", p.stdout)

    def test_init_without_profile_and_without_review_cli_is_claude(self):
        p = self.init(env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.old_behaviour(p, reviewer="claude", review_model="claude-fable-5-1", review_effort="high")
        self.assertIn("의견=Claude(Fable 5.1 high), 재검토=Claude(Fable 5.1 high)", p.stdout)
        self.assertNotIn("전송 동의", p.stdout)
        self.assertIn("의견·재검토: Claude 단독(claude-fable-5-1, high) — 외부 전송 없음", p.stdout)

    def test_init_prints_profile_lines_before_scale_line(self):
        p = self.init(env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        lines = p.stdout.splitlines()
        profile_at = next(i for i, x in enumerate(lines) if x.startswith("프로필:"))
        roles_at = next(i for i, x in enumerate(lines) if x.startswith("역할:"))
        scale_at = next(i for i, x in enumerate(lines) if x.startswith("규모:"))
        self.assertLess(profile_at, roles_at)
        self.assertLess(roles_at, scale_at)

    def test_init_review_model_applies_to_auto_cell_and_is_dropped_for_codex(self):
        script = self.tmp / "run-review.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        p = self.init("--review-model", "claude-opus-5-5", env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.ledger(p)["review_model"], "claude-opus-5-5")
        self.assertIn("덮음: code.opinion·code.recheck·doc.opinion·doc.recheck ← --review-model claude-opus-5-5", p.stdout)
        p = self.init("--review-model", "claude-opus-5-5", env={"CASCADE_RUN_REVIEW": str(script)})
        ledger = self.ledger(p)
        self.assertEqual((ledger["reviewer"], ledger["review_model"], ledger["review_effort"]), ("codex", "", ""))
        self.assertEqual(ledger["profile"]["roles"]["code.opinion"]["model"], "")

    def test_init_bad_review_model_for_claude_exit5_names_option(self):
        p = self.init("--review-model", "composer-2.5", env={"CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("--review-model composer-2.5", p.stdout)
        p = self.init("--reviewer", "claude", "--review-model", "composer-2.5")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("--review-model composer-2.5", p.stdout)

    def test_init_explicit_profile_ignores_invalid_workers_env(self):
        p = self.init("--profile", "claude-only", env={"CASCADE_WORKERS": "bogus"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("CASCADE_WORKERS는 프로필을 명시적으로 골랐으므로 무시한다", p.stdout)
        self.assertIn("프로필: claude-only (--profile)", p.stdout)
        self.assertEqual(self.ledger(p)["profile"]["explicit"], True)

    def test_init_invalid_workers_env_without_profile_exit5(self):
        p = self.init(env={"CASCADE_WORKERS": "bogus"})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertIn("CASCADE_WORKERS=bogus", p.stdout)

    def test_init_env_overrides_are_labelled_and_deprecated(self):
        p = self.init(env={"CASCADE_REVIEWER": "claude", "CASCADE_RUN_REVIEW": str(self.tmp / "none.sh")})
        self.assertIn("참고: 폐기 예정", p.stdout)
        self.assertIn("덮음: code.opinion·code.recheck·doc.opinion·doc.recheck ← CASCADE_REVIEWER=claude", p.stdout)
        self.assertNotIn("← --reviewer", p.stdout)

    def test_init_consent_option_is_validated(self):
        p = self.init("--consent", "openai")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("openai", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        p = self.init("--consent", "none,codex")
        self.assertNotEqual(p.returncode, 0)

    def test_init_unknown_profile_exit5(self):
        p = self.init("--profile", "nope")
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("nope", p.stdout)

    def test_init_unknown_env_profile_exit5(self):
        p = self.init(env={"CASCADE_PROFILE": "nope"})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("claude-only", p.stdout)

    def test_init_known_profile_is_accepted(self):
        p = self.init("--profile", "claude-only")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_init_override_that_breaks_validation_exit5(self):
        fake = self.tmp / "cursor-agent"
        fake.write_text("#!/bin/sh\necho Logged in\n", encoding="utf-8")
        fake.chmod(0o755)
        p = self.init("--workers", "external", "--cursor-model", "claude-sonnet-5-5",
                      env={"CASCADE_CURSOR_AGENT": str(fake)})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("cursor", p.stdout)

    def test_detect_and_review_accept_profile(self):
        p = run_cli(["detect", "--profile", "default", "--no-gh", "--json", "--input", str(self.result)], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["profile"]["name"], "default")
        p = run_cli(["review", "--profile", "default", "--kind", "code", "--uncommitted"], self.repo, env=self.env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_detect_unknown_profile_exit5(self):
        p = run_cli(["detect", "--profile", "nope", "--no-gh", "--json", "--input", str(self.result)], self.repo, env=self.env)
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("nope", p.stdout)
        p = run_cli(["detect", "--no-gh", "--json", "--input", str(self.result)], self.repo,
                    env={**self.env, "CASCADE_PROFILE": "nope"})
        self.assertEqual(p.returncode, 5, p.stdout + p.stderr)


class LocalProfileTest(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.tmp / "none.sh"),
                         "CASCADE_CURSOR_REVIEW": str(self.tmp / "none-cursor.sh")})
        self.result = FIXTURES / "codex-code-e2e.final"

    def init_cli(self, *extra, ok=True):
        return self.cli("init", "--source", "codex-code", "--input", str(self.result), *extra, ok=ok)

    def ledger_json(self, p):
        line = next(x for x in p.stdout.splitlines() if x.startswith("run_dir="))
        return json.loads((Path(line.split("=", 1)[1]) / "ledger.json").read_text(encoding="utf-8"))

    def test_init_local_records_profile(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local")
        ledger = self.ledger_json(p)
        self.assertEqual(ledger["profile"]["name"], "local")
        self.assertEqual(ledger["profile"]["roles"]["code.a"]["runner"], "codex")
        self.assertEqual(ledger["profile"]["roles"]["code.a"]["model"], "gpt-test")
        self.assertEqual(ledger["workers"]["a"]["runner"], "codex")
        self.assertEqual(ledger["workers"]["a"]["model"], "gpt-test")
        self.assertEqual(ledger["workers"]["b"]["runner"], "claude")
        self.assertEqual(ledger["reviewer"], "codex")
        self.assertEqual(ledger["workers_choice"], "external")
        self.assertTrue(ledger["adopt_on_both_pass"])
        self.assertEqual(ledger["profile"]["consented"], ["codex"])
        self.assertEqual((ledger["profile"]["substituted"], ledger["profile"]["overrides"]), ([], []))
        self.assertEqual(ledger["profile"]["first_review"], {})
        self.assertEqual(len(ledger["profile"]["roles"]), 14)
        self.assertIn("프로필: local (--profile)", p.stdout)
        self.assertIn("역할: 첫 검토=Codex", p.stdout)
        self.assertIn("A=Codex(gpt-test low)", p.stdout)
        self.assertIn("전송 동의: 첫 검토·A·의견·재검토 → OpenAI", p.stdout)
        self.assertIn("한쪽 채택", p.stdout)

    def test_init_local_jobs_use_the_profile_workers(self):
        self.use_fakes()
        self.init_cli("--profile", "local")
        run_dir = next(iter((self.repo / ".cascade-fix").glob("cf-*")))
        self.cli("jobs", "--run-dir", str(run_dir), "--round", "1")
        cli = json.loads((run_dir / "round-1" / "stage1-cli-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["runner"]) for j in cli["jobs"]], [("r1-01:a", "codex"), ("r1-02:a", "codex")])
        claude = json.loads((run_dir / "round-1" / "stage1-args.json").read_text(encoding="utf-8"))
        self.assertEqual([(j["id"], j["model"]) for j in claude["jobs"]], [("r1-01:b", HAIKU), ("r1-02:b", HAIKU)])

    def test_init_consent_none_runs_claude_only(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local", "--consent", "none")
        ledger = self.ledger_json(p)
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        self.assertTrue(all(c["fallback"]["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        self.assertEqual(ledger["profile"]["consented"], [])
        self.assertEqual((ledger["reviewer"], ledger["workers_choice"], ledger["workers"]), ("claude", "claude", {}))
        self.assertIn("대체: code.a codex → claude — 전송 동의 없음", p.stdout)
        self.assertNotIn("전송 동의:", p.stdout)
        self.assertIn("역할: 첫 검토=Claude(Fable 5.1 high), A=Claude(Sonnet 5.5 low)", p.stdout)

    def test_init_partial_consent_keeps_only_consented_recipients(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local", "--consent", "cursor")
        ledger = self.ledger_json(p)
        self.assertEqual(ledger["profile"]["consented"], [])
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        p = self.init_cli("--profile", "local", "--consent", "codex,cursor")
        self.assertEqual(self.ledger_json(p)["profile"]["consented"], ["codex"])
        self.assertEqual(self.ledger_json(p)["profile"]["roles"]["code.a"]["runner"], "codex")

    def test_init_local_without_codex_substitutes_with_notice(self):
        p = self.init_cli("--profile", "local")
        ledger = self.ledger_json(p)
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        self.assertIn({"role": "code.a", "from": "codex", "to": "claude", "reason": "미설치 또는 로그인 필요"},
                      ledger["profile"]["substituted"])
        self.assertIn("대체: code.a codex → claude — 미설치 또는 로그인 필요", p.stdout)
        self.assertNotIn("전송 동의", p.stdout)
        self.assertEqual((ledger["reviewer"], ledger["workers"], ledger["workers_choice"]), ("claude", {}, "claude"))
        self.assertEqual(ledger["profile"]["consented"], [])

    def test_init_local_override_is_listed_and_recorded(self):
        self.use_fakes()
        p = self.init_cli("--profile", "local", "--reviewer", "claude")
        ledger = self.ledger_json(p)
        self.assertIn("덮음: code.opinion·code.recheck·doc.opinion·doc.recheck ← --reviewer claude", p.stdout)
        self.assertIn("code.opinion ← --reviewer claude", ledger["profile"]["overrides"])
        self.assertEqual((ledger["reviewer"], ledger["review_model"]), ("claude", "claude-fable-5-1"))

    def test_init_explicit_override_is_not_substituted(self):
        p = self.init_cli("--profile", "claude-only", "--reviewer", "codex")
        ledger = self.ledger_json(p)
        self.assertEqual((ledger["reviewer"], ledger["profile"]["substituted"]), ("codex", []))

    def test_init_workers_external_without_cli_keeps_old_ledger_shape(self):
        p = self.init_cli("--workers", "external", "--reviewer", "claude")
        ledger = self.ledger_json(p)
        self.assertEqual(ledger["workers_choice"], "external")
        self.assertEqual({v["runner"] for v in ledger["workers"].values()}, {"claude"})
        self.assertIn("쓸 수 있는 외부 CLI가 없다", p.stdout)

    def test_init_workers_external_does_not_probe_twice(self):
        self.use_fakes()
        self.init_cli("--workers", "external", "--reviewer", "claude")
        status_calls = sorted((self.tmp / "log").glob("cursor-status-*.args.json"))
        self.assertEqual(len(status_calls), 1)

    def test_detect_local_asks_one_consent_line(self):
        self.use_fakes()
        p = self.cli("detect", "--json", "--no-gh", "--profile", "local", "--input", str(self.result))
        r = json.loads(p.stdout)
        self.assertIn("전송 동의: 첫 검토·A·의견·재검토 → OpenAI", r["approvals"])
        self.assertEqual((r["reviewer"], r["workers"]["a"]["runner"]), ("codex", "codex"))
        self.assertIn("--profile local", r["recommendation"]["steps"][0])
        self.assertEqual(r["profile"]["name"], "local")

    def test_detect_local_without_codex_has_no_openai_line(self):
        p = self.cli("detect", "--json", "--no-gh", "--profile", "local", "--input", str(self.result))
        r = json.loads(p.stdout)
        self.assertFalse(any("OpenAI" in a for a in r["approvals"]))
        self.assertTrue(any("code.a" in n and "codex → claude" in n for n in r["notes"]))
        self.assertEqual(r["workers"], {})


REVIEW_PROFILES = """
[profiles.split]
"code.opinion" = { runner = "claude" }
"code.recheck" = { runner = "codex" }

[profiles.codex-opinion]
"code.opinion" = { runner = "codex" }

[profiles.codex-recheck]
"code.recheck" = { runner = "codex" }

[profiles.cursor-opinion]
"code.opinion" = { runner = "cursor", model = "composer-9" }

[profiles.codex-then-cursor]
"code.opinion" = { runner = "codex", fallback = { runner = "cursor", model = "composer-9" } }

[profiles.sonnet-review]
"code.opinion" = { runner = "claude", model = "claude-sonnet-5-5", effort = "low" }
"code.recheck" = { runner = "claude", model = "claude-haiku-5-5", effort = "low" }
"""
FABLE = "claude-fable-5-1"
SONNET_ID = "claude-sonnet-5-5"
LIMIT_LOG = "You've hit your usage limit\n"
ERROR_LOG = "codex 시작\nERROR: stream disconnected\n"
AUTH_LOG = "codex 시작\nERROR: unexpected status 401 Unauthorized: refresh token was already used\n"


class ReviewRolesBase(ExternalWorkersBase):
    def setUp(self):
        super().setUp()
        toml = Path(self.env["CASCADE_REPOS_TOML"])
        toml.write_text(toml.read_text(encoding="utf-8") + REVIEW_PROFILES, encoding="utf-8")
        self.use_fakes()
        self.rr = self.tmp / "run-review.sh"
        self.cr = self.tmp / "cursor-run-review.sh"
        self.env.update({**NO_LEGACY_ENV, "CASCADE_RUN_REVIEW": str(self.rr), "CASCADE_CURSOR_REVIEW": str(self.cr)})
        self.rd = None

    def next_steps(self, p):
        lines = p.stdout.splitlines()
        return [x.strip() for x in lines[lines.index("다음:") + 1:]]

    def to_adjudicate(self, *init_extra):
        self.init(*init_extra)
        self.rd = self.run_dir / "round-1"
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.rd / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        adj = self.rd / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        return self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))

    def opinion_workflow(self, text="판가름자와 같은 의견이다", model=FABLE, force=False):
        results = review_results(self.rd / "opinion.json", "r1-02:opinion", text, model)
        return self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(results),
                        *(["--force"] if force else []))

    def recheck_workflow(self, model=FABLE):
        text = RECHECK_RESULT.replace("- r1-02: 해소됨 — mul이 곱한다\n", "")
        results = review_results(self.rd / "recheck.json", "round-1:recheck", text, model)
        return self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(results))

    def failed_opinion(self, log_text):
        out = self.rd / "opinion-r1-02.out"
        out.write_text(log_text, encoding="utf-8")
        final = Path(f"{out}.final")
        final.write_text("", encoding="utf-8")
        return self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02", "--result", str(final),
                        ok=False)

    def failed_recheck(self, log_text):
        out = self.rd / "recheck.out"
        out.write_text(log_text, encoding="utf-8")
        final = Path(f"{out}.final")
        final.write_text("", encoding="utf-8")
        return self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(final), ok=False)

    def cell(self, key):
        return self.ledger().profile["roles"][key]

    def substituted(self):
        return [r for r in self.ledger().profile["substituted"] if r["role"].endswith(("opinion", "recheck"))]


class ReviewRolesSplitTest(ReviewRolesBase):
    def test_opinion_and_recheck_runners_split(self):
        p = self.to_adjudicate("--profile", "split")
        steps_after_adjudicate = self.next_steps(p)
        self.assertIn("Workflow 실행", steps_after_adjudicate[0])
        self.assertIn("opinion-args.json", steps_after_adjudicate[0])
        [job] = json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("r1-02:opinion", FABLE, "high"))
        self.opinion_workflow()
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        steps_after_apply = self.next_steps(p)
        self.assertIn("--recheck", steps_after_apply[0])
        self.assertIn(shlex.quote(str(self.rr)), steps_after_apply[0])
        self.assertNotIn("Workflow", steps_after_apply[0])
        self.assertFalse((self.rd / "recheck-args.json").exists())
        self.assertEqual(self.ledger().reviewer, "claude")

    def test_cursor_review_command_passes_the_cell_model(self):
        p = self.to_adjudicate("--profile", "cursor-opinion")
        [line] = [x for x in self.next_steps(p) if " --opinion " in x]
        self.assertTrue(line.startswith(shlex.quote(str(self.cr)) + " --opinion"), line)
        self.assertIn("--model composer-9", line)

    def test_codex_review_command_has_no_model(self):
        p = self.to_adjudicate("--profile", "codex-opinion")
        [line] = [x for x in self.next_steps(p) if " --opinion " in x]
        self.assertTrue(line.startswith(shlex.quote(str(self.rr)) + " --opinion"), line)
        self.assertNotIn("--model", line)

    def test_opinion_model_warning_uses_cell(self):
        self.to_adjudicate("--profile", "sonnet-review")
        [job] = json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (SONNET_ID, "low"))
        p = self.opinion_workflow(model=SONNET_ID)
        self.assertNotIn("경고", p.stdout)
        p = self.opinion_workflow(model=FABLE, force=True)
        self.assertIn("경고", p.stdout)
        self.assertIn(SONNET_ID, p.stdout)

    def test_recheck_model_warning_uses_cell(self):
        self.to_adjudicate("--profile", "sonnet-review")
        self.opinion_workflow(model=SONNET_ID)
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        [job] = json.loads((self.rd / "recheck-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["model"], job["effort"]), (HAIKU, "low"))
        p = self.recheck_workflow(model=FABLE)
        self.assertIn("경고", p.stdout)
        self.assertIn(HAIKU, p.stdout)

    def test_recheck_matching_model_has_no_warning(self):
        self.to_adjudicate("--profile", "sonnet-review")
        self.opinion_workflow(model=SONNET_ID)
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.recheck_workflow(model=HAIKU)
        self.assertNotIn("경고", p.stdout)


class ReviewRolesFallbackTest(ReviewRolesBase):
    def test_opinion_limit_falls_back_to_claude(self):
        self.to_adjudicate("--profile", "codex-opinion")
        p = self.failed_opinion(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [job] = json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("r1-02:opinion", FABLE, "high"))
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertEqual((self.cell("code.opinion")["runner"], self.cell("code.opinion")["model"]), ("claude", FABLE))
        self.assertEqual(self.ledger().reviewer, "claude")
        self.assertNotIn("r1-02", self.ledger().opinions)
        self.assertIn("대체", p.stdout)
        self.assertIn("Workflow 실행", self.next_steps(p)[0])
        self.assertIn("opinion-args.json", self.next_steps(p)[0])
        p = self.opinion_workflow()
        self.assertEqual(self.ledger().opinions["r1-02"], "판가름자와 같은 의견이다")
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("검토자: Claude", p.stdout)
        self.assertIn("Claude 의견:", p.stdout)
        self.assertNotIn("Codex", p.stdout.split("## 사람이 볼 목록")[0])

    def test_opinion_error_falls_back_with_error_reason(self):
        self.to_adjudicate("--profile", "codex-opinion")
        p = self.failed_opinion(ERROR_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "실행 오류"}])
        self.assertTrue((self.rd / "opinion-args.json").is_file())

    def test_opinion_fallback_only_writes_still_pending_opinions(self):
        self.to_adjudicate("--profile", "codex-opinion")
        (self.rd / "opinion-r1-01.md").write_text("판정 의견 요청\n", encoding="utf-8")
        self.failed_opinion(LIMIT_LOG)
        ids = [j["id"] for j in json.loads((self.rd / "opinion-args.json").read_text(encoding="utf-8"))["jobs"]]
        self.assertEqual(ids, ["r1-01:opinion", "r1-02:opinion"])

    def test_opinion_auth_still_stops(self):
        self.to_adjudicate("--profile", "codex-opinion")
        p = self.failed_opinion(AUTH_LOG)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("codex login", p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [])
        self.assertEqual(self.cell("code.opinion")["runner"], "codex")
        self.assertFalse((self.rd / "opinion-args.json").exists())

    def test_opinion_without_log_still_stops(self):
        self.to_adjudicate("--profile", "codex-opinion")
        out = self.rd / "opinion-r1-02.out"
        p = self.cli("opinion", "--run-dir", str(self.run_dir), "--round", "1", "--id", "r1-02",
                     "--result", f"{out}.final", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("실행 로그도 없다", p.stdout)
        self.assertEqual(self.substituted(), [])

    def test_opinion_fallback_to_cursor_prints_cr_with_model(self):
        self.to_adjudicate("--profile", "codex-then-cursor")
        p = self.failed_opinion(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "cursor", "reason": "이용 한도"}])
        [line] = [x for x in self.next_steps(p) if " --opinion " in x]
        self.assertTrue(line.startswith(shlex.quote(str(self.cr)) + " --opinion"), line)
        self.assertIn("--model composer-9", line)
        self.assertFalse((self.rd / "opinion-args.json").exists())
        self.assertEqual(self.cell("code.opinion")["fallback"]["runner"], "claude")
        self.assertEqual(self.ledger().reviewer, "cursor")

    def test_opinion_fallback_outside_consent_is_claude_only(self):
        self.to_adjudicate("--profile", "codex-then-cursor", "--consent", "codex")
        self.assertEqual(self.cell("code.opinion")["fallback"]["runner"], "claude")
        self.failed_opinion(LIMIT_LOG)
        self.assertEqual(self.substituted()[-1]["to"], "claude")
        self.assertTrue((self.rd / "opinion-args.json").is_file())

    def test_recheck_limit_falls_back_to_claude(self):
        self.to_adjudicate("--profile", "codex-recheck")
        self.opinion_workflow()
        p = self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        self.assertIn("--recheck", self.next_steps(p)[0])
        p = self.failed_recheck(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        [job] = json.loads((self.rd / "recheck-args.json").read_text(encoding="utf-8"))["jobs"]
        self.assertEqual((job["id"], job["model"], job["effort"]), ("round-1:recheck", FABLE, "high"))
        self.assertEqual(self.substituted(), [{"role": "code.recheck", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertEqual(self.ledger().reviewer, "claude")
        self.assertIn("Workflow 실행", self.next_steps(p)[0])
        self.assertIn("recheck-args.json", self.next_steps(p)[0])
        self.assertFalse(any(e["step"] == "recheck" for e in self.ledger().rounds[0].events))
        text = RECHECK_RESULT.replace("- r1-02: 해소됨 — mul이 곱한다\n", "")
        ok = review_results(self.rd / "recheck.json", "round-1:recheck", text)
        p = self.cli("recheck", "--run-dir", str(self.run_dir), "--round", "1", "--result", str(ok))
        self.assertIn("status=종료", p.stdout)
        p = self.cli("report", "--run-dir", str(self.run_dir))
        self.assertIn("| Claude 재검토 | 1 |", p.stdout)

    def test_recheck_auth_still_stops(self):
        self.to_adjudicate("--profile", "codex-recheck")
        self.opinion_workflow()
        self.cli("apply", "--run-dir", str(self.run_dir), "--round", "1")
        p = self.failed_recheck(AUTH_LOG)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("codex login", p.stdout + p.stderr)
        self.assertEqual(self.substituted(), [])

    def test_legacy_ledger_without_profile_still_falls_back(self):
        self.init()
        self.cli("jobs", "--run-dir", str(self.run_dir), "--round", "1")
        path = self.run_dir / "ledger.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(profile={}, reviewer="codex", review_model="", review_effort="")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.rd = self.run_dir / "round-1"
        for name in ("r1-01-a", "r1-01-b"):
            wt = self.wt(name)
            (wt / "calc.py").write_text(fix(CALC, "return a - b", "return a + b"), encoding="utf-8")
            commit_worktree(wt, name)
        stage1 = self.rd / "stage1.json"
        shutil.copyfile(FIXTURES / "workflow-stage1.json", stage1)
        self.cli("judge", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(stage1))
        adj = self.rd / "adj.json"
        adj.write_text(json.dumps({"results": [{"id": "r1-02:adjudicator", "output": None}]}), encoding="utf-8")
        p = self.cli("adjudicate", "--run-dir", str(self.run_dir), "--round", "1", "--results", str(adj))
        self.assertIn("--opinion", p.stdout)
        p = self.failed_opinion(LIMIT_LOG)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        ledger = self.ledger()
        self.assertEqual((ledger.profile["name"], ledger.profile["source"]), ("legacy", "legacy"))
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "claude")
        self.assertEqual(ledger.profile["roles"]["code.recheck"]["runner"], "codex")
        self.assertEqual(self.substituted(), [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertIn("Workflow 실행", self.next_steps(p)[0])


class ReviewHelpersTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)

    def ledger(self, reviewer="codex", profile=None):
        return Ledger(run_id="x", source="codex-code", mode="code", root="/r", snapshot={}, dry_run=False,
                      reviewer=reviewer, profile=profile or {})

    def profile(self, fallback=None, consented=("codex", "cursor")):
        roles = profiles.builtin("claude-only")["roles"]
        roles["code.opinion"] = {**profiles.reset_cell("code.opinion", "codex"),
                                 "fallback": fallback or roles["code.opinion"]["fallback"]}
        return {"name": "t", "source": "--profile", "explicit": True, "roles": roles, "overrides": [], "substituted": [],
                "consented": list(consented), "first_review": {}}

    def result(self, log_text=None, final=None):
        out = self.tmp / "r.out"
        final_path = Path(f"{out}.final")
        if log_text is not None:
            out.write_text(log_text, encoding="utf-8")
        if final is not None:
            final_path.write_text(final, encoding="utf-8")
        return final_path

    def test_external_failure_classification(self):
        self.assertIsNone(cli_steps.external_failure(self.result()))
        self.assertIsNone(cli_steps.external_failure(self.result(final="")))
        self.assertIsNone(cli_steps.external_failure(self.result(LIMIT_LOG, final="의견")))
        self.assertEqual(cli_steps.external_failure(self.result(LIMIT_LOG, final="")), "limit")
        self.assertEqual(cli_steps.external_failure(self.result(ERROR_LOG, final="\n")), "error")
        self.assertEqual(cli_steps.external_failure(self.result(AUTH_LOG, final="")), "auth")
        self.assertEqual(cli_steps.external_failure(self.result("Authentication required\n" + LIMIT_LOG)), "auth")
        self.assertIsNone(cli_steps.external_failure(self.tmp / "plain.txt"))

    def test_review_cell_reads_the_mode_cell_and_materializes_legacy(self):
        ledger = self.ledger(reviewer="cursor")
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["runner"], "cursor")
        self.assertEqual(cli_steps.review_cell(ledger, "recheck")["runner"], "cursor")
        self.assertEqual(ledger.profile, {})
        ledger = self.ledger(profile=self.profile())
        self.assertEqual(cli_steps.review_cell(ledger, "opinion")["runner"], "codex")
        self.assertEqual(cli_steps.review_cell(ledger, "recheck")["runner"], "claude")
        self.assertTrue(cli_steps.is_claude(ledger, "recheck"))
        self.assertFalse(cli_steps.is_claude(ledger, "opinion"))
        self.assertEqual(cli_steps.review_script(ledger, "opinion"), cli_steps.RUN_REVIEW)
        ledger.profile["roles"]["code.opinion"]["runner"] = "cursor"
        self.assertEqual(cli_steps.review_script(ledger, "opinion"), cli_steps.CURSOR_REVIEW)

    def test_substitute_review_records_and_returns_new_cell(self):
        ledger = self.ledger(profile=self.profile())
        new = cli_steps.substitute_review(ledger, "opinion", "이용 한도")
        self.assertEqual((new["runner"], new["model"], new["effort"]), ("claude", FABLE, "high"))
        self.assertEqual(ledger.profile["roles"]["code.opinion"], new)
        self.assertEqual(ledger.profile["substituted"],
                         [{"role": "code.opinion", "from": "codex", "to": "claude", "reason": "이용 한도"}])
        self.assertEqual((ledger.reviewer, ledger.review_model, ledger.review_effort), ("claude", FABLE, "high"))
        self.assertEqual(ledger.profile["consented"], ["codex", "cursor"])

    def test_substitute_review_materializes_a_legacy_ledger(self):
        ledger = self.ledger(reviewer="codex")
        new = cli_steps.substitute_review(ledger, "recheck", "실행 오류")
        self.assertEqual(new["runner"], "claude")
        self.assertEqual(ledger.profile["name"], "legacy")
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["runner"], "codex")
        self.assertEqual(ledger.profile["roles"]["code.recheck"]["runner"], "claude")
        self.assertEqual(ledger.profile["substituted"],
                         [{"role": "code.recheck", "from": "codex", "to": "claude", "reason": "실행 오류"}])
        self.assertEqual(ledger.reviewer, "codex")

    def test_substitute_review_respects_consent_and_exhaustion(self):
        cursor = {"runner": "cursor", "model": "composer-9", "effort": ""}
        ledger = self.ledger(profile=self.profile(fallback=cursor))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "x")["runner"], "cursor")
        self.assertEqual(ledger.reviewer, "cursor")
        self.assertEqual(ledger.profile["roles"]["code.opinion"]["fallback"]["runner"], "claude")
        ledger = self.ledger(profile=self.profile(fallback=cursor, consented=("codex",)))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "x")["runner"], "claude")
        same = {"runner": "codex", "model": "", "effort": ""}
        ledger = self.ledger(profile=self.profile(fallback=same))
        self.assertEqual(cli_steps.substitute_review(ledger, "opinion", "x")["runner"], "claude")


if __name__ == "__main__":
    unittest.main()
