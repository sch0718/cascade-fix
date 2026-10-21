import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_repo, run_cli, write_repos_toml  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NO_LEGACY_ENV = {"CASCADE_WORKERS": "", "CASCADE_REVIEWER": ""}


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

    def test_init_without_profile_is_unchanged(self):
        p = self.init()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("폐기 예정", p.stdout)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("지적 2건", p.stdout)

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
        self.assertNotIn("unrecognized arguments", p.stderr)
        p = run_cli(["review", "--profile", "default", "--kind", "code", "--uncommitted"], self.repo, env=self.env)
        self.assertNotIn("unrecognized arguments", p.stderr)


if __name__ == "__main__":
    unittest.main()
