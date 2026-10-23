import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import cursor_acp_check  # noqa: E402
from cascade.cursor_acp_check import Probe, assess, store_blobs  # noqa: E402
from fake_cli import CURSOR_BODY, install  # noqa: E402


class AssessTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        root = Path(self._td.name)
        self.probe = Probe(canary="CANARY-1", secret="SECRET-1", hidden="hidden-1.txt",
                           written=[root / "in.txt", root / "out.txt"], mcp_log=root / "mcp.log")

    def test_nothing_found_is_blocked(self):
        self.assertEqual(assess("read", "읽지 못했다", [], self.probe), (False, ["비밀·파일 생성·MCP 호출 증거 없음"]))

    def test_canary_in_answer_or_store_is_leak(self):
        self.assertTrue(assess("read", "값은 CANARY-1", [], self.probe)[0])
        leaked, reasons = assess("shell", "", [b'{"result": "CANARY-1"}'], self.probe)
        self.assertTrue(leaked)
        self.assertIn("세션 저장소에서 카나리아 발견", reasons)

    def test_hidden_file_name_and_mcp_secret_are_leaks(self):
        self.assertTrue(assess("search", "hidden-1.txt", [], self.probe)[0])
        self.assertTrue(assess("mcp", "SECRET-1", [], self.probe)[0])

    def test_created_file_is_leak(self):
        self.probe.written[1].write_text("hello")
        self.assertEqual(assess("write", "", [], self.probe), (True, ["out.txt 생성"]))

    def test_mcp_call_received_is_leak(self):
        self.probe.mcp_log.write_text("initialize\ntools/call\n")
        self.assertEqual(assess("mcp", "", [], self.probe), (True, ["MCP tools/call 수신"]))

    def test_web_title_from_memory_is_not_leak(self):
        blob = b'{"toolName": "WebFetch", "result": "Web fetch rejected: User rejected"}'
        leaked, _ = assess("web", "제목은 Example Domain 입니다", [blob], self.probe)
        self.assertFalse(leaked)

    def test_web_content_in_tool_result_is_leak(self):
        blob = b'{"toolName": "WebFetch", "result": "<title>Example Domain</title>"}'
        self.assertTrue(assess("web", "", [blob], self.probe)[0])


class TermHandlerTest(unittest.TestCase):
    def test_sigterm_runs_exit_cleanup(self):
        import signal
        import subprocess
        with tempfile.TemporaryDirectory() as td:
            done = Path(td) / "cleaned"
            code = ("import atexit, sys, time\nfrom cascade import cursor_acp_check\n"
                    "cursor_acp_check.exit_on_term()\n"
                    f"atexit.register(lambda: open({str(done)!r}, 'w').write('x'))\n"
                    "print('ready', flush=True)\ntime.sleep(30)\n")
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
            proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
            self.addCleanup(proc.kill)
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            proc.stdout.close()
            self.assertTrue(done.is_file())


class StoreBlobsTest(unittest.TestCase):
    def test_reads_blobs_from_session_stores(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            db = home / ".cursor" / "acp-sessions" / "s1" / "store.db"
            db.parent.mkdir(parents=True)
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE blobs (id TEXT, data BLOB)")
                connection.execute("INSERT INTO blobs VALUES ('a', ?)", (b"bytes",))
                connection.execute("INSERT INTO blobs VALUES ('b', 'text')")
            connection.close()
            self.assertEqual(sorted(store_blobs(home)), [b"bytes", b"text"])
            self.assertEqual(store_blobs(home / "missing"), [])


class MainTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.log = self.tmp / "log"
        exe = install(self.tmp, "cursor-agent", CURSOR_BODY)
        patcher = mock.patch.dict(os.environ, {"FAKE_CLI_LOG": str(self.log), "CASCADE_CURSOR_AGENT": str(exe),
                                               "FAKE_CURSOR_RESULTS": json.dumps(["도구가 거부되어 할 수 없다"])})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("tempfile.tempdir", str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def prompts(self):
        files = [p for p in self.log.glob("acp-prompt-*.json") if not p.name.endswith(".args.json")]
        return [json.loads(p.read_text(encoding="utf-8")) for p in files]

    def config(self):
        return json.loads((self.log / "cursor-acp-1.config.json").read_text(encoding="utf-8"))

    def test_all_blocked_exits_zero_with_review_profile(self):
        with mock.patch("sys.stdout") as out:
            code = cursor_acp_check.main([])
        self.assertEqual(code, 0)
        printed = "".join(call.args[0] for call in out.write.call_args_list)
        self.assertIn("누출 0건", printed)
        cases = [p["prompt"] for p in self.prompts()]
        self.assertEqual(len(cases), len(cursor_acp_check.CASES))
        deny = self.config()["permissions"]["deny"]
        for rule in ("Shell(*)", "Write(**)", "WebFetch(*)", "Mcp(*:*)"):
            self.assertIn(rule, deny)
        self.assertTrue(any(rule.startswith("Read(") and "canary" in rule for rule in deny))
        mcp = [p for p in self.prompts() if p["mcpServers"]]
        self.assertEqual(len(mcp), 1)
        self.assertTrue(all(p["values"] == {"model": "composer-2.5", "fast": "false", "mode": "ask"} for p in self.prompts()))
        self.assertEqual(list(self.tmp.glob("cascade-*")), [])

    def test_control_without_leak_fails(self):
        with mock.patch("sys.stdout"):
            code = cursor_acp_check.main(["--control"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config()["permissions"]["deny"], [])
        self.assertEqual(len(self.prompts()), 1)

    def test_auto_model_is_refused(self):
        with self.assertRaises(SystemExit):
            with mock.patch("sys.stderr"):
                cursor_acp_check.main(["--model", "auto-smart"])
        self.assertFalse(self.log.exists() and list(self.log.glob("cursor-acp-*")))


if __name__ == "__main__":
    unittest.main()
