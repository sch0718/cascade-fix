import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import verify  # noqa: E402
from cascade.config import RepoConfig  # noqa: E402
from cascade.findings import Finding  # noqa: E402
from helpers import make_repo  # noqa: E402


def finding(body):
    return Finding(id="f1", source="pr", severity="Warning", location="a.py:1", body=body, evidence="")


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_related_tests_by_dir_name_and_import(self):
        repo = make_repo(self.tmp / "repo", {
            "src/pkg/mod.py": "x = 1\n",
            "src/pkg/test_local.py": "pass\n",
            "src/pkg/other.py": "pass\n",
            "tests/test_mod.py": "pass\n",
            "tests/test_importer.py": "from pkg.mod import x\n",
            "tests/test_unrelated.py": "import os\n",
            "web/util.ts": "export const a = 1\n",
            "web/util.test.ts": "x\n",
            "web/page.spec.ts": "import { a } from './util'\n",
            "web/node_modules/dep/util.test.js": "x\n",
        })
        py = verify.related_tests(repo, ["src/pkg/mod.py"])
        self.assertEqual(py, ["src/pkg/test_local.py", "tests/test_importer.py", "tests/test_mod.py"])
        ts = verify.related_tests(repo, ["web/util.ts"])
        self.assertEqual(ts, ["web/page.spec.ts", "web/util.test.ts"])

    def test_run_verifiers_worker_stage_substitutes_files_and_reports_tail(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n", "test_a.py": "pass\n"})
        cfg = RepoConfig(
            tests=["python3 -c 'import sys; print(\"boom\"); sys.exit(1)' {files}"],
            build=["true"],
            lint=["true"],
            format=["true"],
        )
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertEqual(len(r), 4)
        self.assertFalse(r[0].ok)
        self.assertIn("test_a.py", r[0].cmd)
        self.assertIn("boom", r[0].tail)
        self.assertTrue(all(x.ok for x in r[1:]))
        self.assertEqual(set(r[0].to_dict()), {"cmd", "ok", "tail"})

    def test_run_verifiers_without_related_tests_still_runs_command(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(tests=["true {files}"])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].ok)
        self.assertNotIn("{files}", r[0].cmd)

    def test_final_stage_runs_only_full_tests(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(tests=["false"], full_tests=["true"], build=["false"], lint=["false"])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "final")
        self.assertEqual([x.cmd for x in r], ["true"])
        self.assertTrue(r[0].ok)

    def test_timeout_counts_as_failure(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(full_tests=["sleep 5"])
        with mock.patch.object(verify, "TIMEOUT", 0.2):
            r = verify.run_verifiers(repo, cfg, [], "final")
        self.assertFalse(r[0].ok)
        self.assertEqual(r[0].tail, "시간 초과(600초)")

    def test_run_format_ignores_exit_codes(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        cfg = RepoConfig(format=["false", "touch formatted.txt"])
        self.assertIsNone(verify.run_format(repo, cfg))
        self.assertTrue((repo / "formatted.txt").exists())

    def test_check_doc_refs_lists_missing_paths(self):
        repo = make_repo(self.tmp / "repo", {"app.py": "x=1\n", "docs/real.md": "r\n"})
        doc = repo / "docs" / "guide.md"
        doc.write_text(
            "코드는 app.py:12 를 본다.\n"
            "`ghost.py` 는 없다. `docs/real.md` 는 있다.\n"
            "또 missing/dir/x.ts:3 도 없다.\n"
            "https://example.com/a.py 와 `src/*.py`, `{x}/y.py`, `외부/copy/z.py` 는 무시.\n",
            encoding="utf-8",
        )
        self.assertEqual(verify.check_doc_refs(repo, doc), ["ghost.py", "missing/dir/x.ts"])

    def test_check_snippets_runs_configured_checker_per_language(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        doc = repo / "doc.md"
        doc.write_text(
            "```python\nx = 1\n```\n\n```python\nx = (\n```\n\n```js\nvar a;\n```\n\n```\nplain\n```\n",
            encoding="utf-8",
        )
        cfg = RepoConfig(doc_snippet_check={"python": "python3 -m py_compile {file}"})
        r = verify.check_snippets(repo, doc, cfg)
        self.assertEqual(len(r), 2)
        self.assertTrue(r[0].ok)
        self.assertFalse(r[1].ok)
        self.assertIn("0", r[0].cmd)
        self.assertIn("1", r[1].cmd)
        self.assertTrue((repo / ".cascade-fix" / "snippets" / "0.python").exists())

    def test_repro_cmds_from_confirmed_block_only(self):
        body = (
            "설명입니다.\n\n"
            "```sh\nignored --cmd\n```\n\n"
            "**확인한 내용**: 재현했다.\n"
            "```sh\n$ python3 -c 'print(1)'\n\nls -la\n```\n\n"
            "**근거**: 위\n```sh\nnot-this\n```\n"
        )
        self.assertEqual(verify.repro_cmds(finding(body)), ["python3 -c 'print(1)'", "ls -la"])
        self.assertEqual(verify.repro_cmds(finding("```sh\nfoo\n```\n")), [])

    def test_repro_cmds_spans_blank_line_until_evidence_or_reply(self):
        body = (
            "**확인한 내용**:\n\n"
            "```sh\n$ python3 -c 'print(1)'\nls -la\n```\n\n"
            "**근거**: 위\n\n```sh\nnot-this\n```\n"
        )
        self.assertEqual(verify.repro_cmds(finding(body)), ["python3 -c 'print(1)'", "ls -la"])
        reply = "**확인한 내용**: 재현\n\n```sh\nfoo\n```\n\n답글(작성자):\n```sh\nnot-this\n```\n"
        self.assertEqual(verify.repro_cmds(finding(reply)), ["foo"])

    def test_related_tests_from_package_and_relative_imports(self):
        repo = make_repo(self.tmp / "repo", {
            "cascade/verify.py": "x = 1\n",
            "tests/test_pkg_style.py": "from cascade import verify\n",
            "tests/test_multi.py": "from cascade import config, verify\n",
            "tests/test_other.py": "from cascade import config\n",
            "cascade/test_rel_a.py": "from .verify import x\n",
            "pkg/mod.py": "x = 1\n",
            "pkg/test_rel_b.py": "from . import mod\n",
            "pkg/test_rel_c.py": "from .other import y\n",
        })
        self.assertEqual(
            verify.related_tests(repo, ["cascade/verify.py"]),
            ["cascade/test_rel_a.py", "tests/test_multi.py", "tests/test_pkg_style.py"],
        )
        self.assertEqual(verify.related_tests(repo, ["pkg/mod.py"]), ["pkg/test_rel_b.py", "pkg/test_rel_c.py"])

    def test_related_tests_includes_spec_named_file_under_tests_dir(self):
        repo = make_repo(self.tmp / "repo", {"web/util.ts": "x\n", "tests/util.spec.ts": "x\n"})
        self.assertEqual(verify.related_tests(repo, ["web/util.ts"]), ["tests/util.spec.ts"])

    def test_run_verifiers_quotes_related_test_paths(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n", "test_a b.py": "pass\n"})
        cfg = RepoConfig(tests=["python3 -c 'import sys; print(sys.argv[1:])' {files}"])
        r = verify.run_verifiers(repo, cfg, ["a.py"], "worker")
        self.assertTrue(r[0].ok)
        self.assertIn("['test_a b.py']", r[0].tail)

    def test_run_verifiers_survives_non_utf8_output(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        cfg = RepoConfig(full_tests=["python3 -c \"import sys; sys.stdout.buffer.write(b'\\xff\\xfe')\""])
        r = verify.run_verifiers(repo, cfg, [], "final")
        self.assertEqual(len(r), 1)
        self.assertTrue(r[0].ok)

    def test_commands_do_not_inherit_stdin(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        cfg = RepoConfig(full_tests=["cat; echo done"])
        r = verify.run_verifiers(repo, cfg, [], "final")
        self.assertTrue(r[0].ok)
        self.assertIn("done", r[0].tail)

    def test_check_doc_refs_ignores_paths_outside_worktree(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x\n"})
        doc = repo / "doc.md"
        doc.write_text("`../outside.py` `/nonexistent/abs.py` `ghost.py` http://h/x.py:3\n", encoding="utf-8")
        self.assertEqual(verify.check_doc_refs(repo, doc), ["ghost.py"])

    def test_run_repro_requires_fail_before_and_pass_after(self):
        base = self.tmp / "base"
        fix = self.tmp / "fix"
        base.mkdir()
        fix.mkdir()
        (fix / "ok.flag").write_text("1")
        r = verify.run_repro(base, fix, ["test -f ok.flag", "true", "false"])
        self.assertEqual([x.ok for x in r], [True, False, False])
        self.assertIn("수정 전 rc=1 / 수정 후 rc=0", r[0].tail)
        self.assertIn("수정 전 rc=0 / 수정 후 rc=0", r[1].tail)


if __name__ == "__main__":
    unittest.main()
