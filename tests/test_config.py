import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import config  # noqa: E402
from helpers import make_repo, run_cli, write_repos_toml  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"README.md": "x"})

    def test_loads_commands_for_root(self):
        cfg = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["pytest {files}"], "full_tests": ["pytest"], "format": ["ruff format"]}})
        rc = config.load_repo_config(self.repo, cfg)
        self.assertEqual(rc.tests, ["pytest {files}"])
        self.assertEqual(rc.full_tests, ["pytest"])
        self.assertEqual(rc.format, ["ruff format"])
        self.assertEqual(rc.lint, [])
        self.assertEqual(rc.doc_snippet_check, {})

    def test_default_sensitive_paths(self):
        cfg = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["pytest"]}})
        rc = config.load_repo_config(self.repo, cfg)
        self.assertEqual(rc.sensitive_paths, ["**/auth*", "**/security*", "**/*delete*", "**/migrations/**"])

    def test_overrides_sensitive_paths_and_doc_snippet_check(self):
        cfg = write_repos_toml(self.tmp, {str(self.repo): {"sensitive_paths": ["**/pay*"], "doc_snippet_check": {"python": "python -m py_compile {file}"}}})
        rc = config.load_repo_config(self.repo, cfg)
        self.assertEqual(rc.sensitive_paths, ["**/pay*"])
        self.assertEqual(rc.doc_snippet_check, {"python": "python -m py_compile {file}"})

    def test_setup_field_loads(self):
        cfg = write_repos_toml(self.tmp, {str(self.repo): {"tests": ["pytest"], "setup": ["ln -s {root}/node_modules node_modules"]}})
        self.assertEqual(config.load_repo_config(self.repo, cfg).setup, ["ln -s {root}/node_modules node_modules"])

    def test_string_instead_of_list_is_rejected(self):
        cfg = write_repos_toml(self.tmp, {str(self.repo): {"tests": "pytest"}})
        with self.assertRaises(config.ConfigError):
            config.load_repo_config(self.repo, cfg)
        p = run_cli(["init", "--source", "codex-code", "--input", str(cfg)], cwd=self.repo,
                    env={"CASCADE_REPOS_TOML": str(cfg)})
        self.assertEqual(p.returncode, 5, p.stdout)
        self.assertIn("문자열 목록", p.stdout)

    def test_sensitive_path_matches_root_and_case(self):
        pats = config.DEFAULT_SENSITIVE_PATHS
        for path in ("auth.py", "auth/handler.go", "migrations/0001.sql", "src/AuthService.ts", "SECURITY.md",
                     "lib/user_delete.py", "src/author.py"):
            self.assertTrue(config.is_sensitive_path(path, pats), path)
        for path in ("src/calc.py", "docs/guide.md"):
            self.assertFalse(config.is_sensitive_path(path, pats), path)
        self.assertFalse(config.is_sensitive_path("src/author.py", ["**/auth/**", "auth.*", "**/auth.*"]))

    def test_returns_none_when_root_missing(self):
        cfg = write_repos_toml(self.tmp, {"/some/other/repo": {"tests": ["pytest"]}})
        self.assertIsNone(config.load_repo_config(self.repo, cfg))

    def test_returns_none_when_file_missing(self):
        self.assertIsNone(config.load_repo_config(self.repo, self.tmp / "nope.toml"))

    def test_detect_candidates_reads_package_json_scripts(self):
        root = make_repo(self.tmp / "node", {"package.json": '{"scripts":{"test":"vitest","lint":"eslint ."}}'})
        c = config.detect_candidates(root)
        self.assertEqual(c["tests"], ["npm run test"])
        self.assertEqual(c["full_tests"], ["npm run test"])
        self.assertEqual(c["lint"], ["npm run lint"])
        self.assertEqual(c["build"], [])

    def test_detect_candidates_reads_pyproject(self):
        root = make_repo(self.tmp / "py", {"pyproject.toml": "[tool.pytest.ini_options]\n[tool.ruff]\n[tool.mypy]\n"})
        c = config.detect_candidates(root)
        self.assertEqual(c["tests"], ["pytest {files}"])
        self.assertEqual(c["full_tests"], ["pytest"])
        self.assertEqual(c["lint"], ["ruff check ."])
        self.assertEqual(c["format"], ["ruff format {files}"])
        self.assertEqual(c["typecheck"], ["mypy ."])

    def test_detect_candidates_reads_makefile_targets(self):
        root = make_repo(self.tmp / "mk", {"Makefile": "test:\n\tpytest\n\nlint:\n\truff .\n"})
        c = config.detect_candidates(root)
        self.assertEqual(c["tests"], ["make test"])
        self.assertEqual(c["lint"], ["make lint"])
        self.assertEqual(c["build"], [])

    def test_detect_candidates_empty_repo(self):
        c = config.detect_candidates(self.repo)
        self.assertEqual(c, {k: [] for k in ("tests", "full_tests", "build", "typecheck", "lint", "format")})

    def test_exit_codes(self):
        self.assertEqual((config.ExitCodes.NO_VERIFY_CONFIG, config.ExitCodes.PARSE_ERROR, config.ExitCodes.NEEDS_USER), (4, 5, 6))

    def test_missing_verify_config_exits_4(self):
        result_file = self.tmp / "result.json"
        result_file.write_text("{}", encoding="utf-8")
        env = {"CASCADE_REPOS_TOML": str(self.tmp / "none.toml")}
        p = run_cli(["init", "--source", "codex-code", "--input", str(result_file)], cwd=self.repo, env=env)
        self.assertEqual(p.returncode, 4)
        self.assertIn("검증 명령", p.stdout)


if __name__ == "__main__":
    unittest.main()
