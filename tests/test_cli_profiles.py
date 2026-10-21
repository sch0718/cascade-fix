import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_repo, run_cli, write_repos_toml  # noqa: E402
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
        self.assertEqual(ledger["profile"]["consented"], ["cursor"])
        self.assertTrue(all(c["runner"] == "claude" for c in ledger["profile"]["roles"].values()))
        p = self.init_cli("--profile", "local", "--consent", "codex,cursor")
        self.assertEqual(self.ledger_json(p)["profile"]["consented"], ["codex", "cursor"])
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


if __name__ == "__main__":
    unittest.main()
