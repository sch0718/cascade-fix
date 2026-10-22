import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import adopt_recalc  # noqa: E402
import score  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "adopt-recalc" / "both-pass"
SAMPLE = {"x": {"id": "x", "finding": {"id": "r1-01"}, "truth": {"verdict": "사실"}, "baseline_opus_calls": 1}}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class AdoptRecalcTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.runs = self.tmp / "runs"
        shutil.copytree(FIXTURE, self.runs)
        self.out = self.tmp / "out"

    def round_dir(self, base=None):
        return (base or self.runs) / "x" / "run" / "round-1"

    def recalc(self, runs=None):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            code = adopt_recalc.main(["--runs", str(runs or self.runs), "--out", str(self.out)])
        return code, buf.getvalue()

    def test_recalc_flips_both_pass_item_and_removes_opus_usage(self):
        off = score.compute(self.runs, SAMPLE, cost_scope="round1-core")
        self.assertEqual(self.recalc()[0], 0)
        on = score.compute(self.out, SAMPLE, cost_scope="round1-core")
        self.assertEqual(off["opus_calls"] - on["opus_calls"], 1)
        self.assertLess(on["claude_cost"], off["claude_cost"])
        self.assertEqual(on["cost_missing"], [])
        check = read(self.out / "check.json")
        self.assertTrue(check["ok"])
        self.assertEqual(check["flipped"], 1)
        self.assertEqual(check["cost_scope"], "round1-core")
        ledger = read(self.out / "x" / "ledger.json")
        self.assertIn("둘 다 검증 통과", " ".join(ledger["rounds"][0]["decisions"][0]["reasons"]))
        self.assertEqual(ledger["rounds"][0]["decisions"][0]["patch_from"], "a")
        self.assertNotIn("adjudicate", [e["step"] for e in ledger["rounds"][0]["events"]])
        self.assertEqual(read(self.out / "counterfactual.json"), {"cost_scope": "round1-core"})
        row = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["off"]["outcome"], row["on"]["outcome"]), ("판가름", "합의-사실"))

    def test_recalc_removes_every_adjudication_trace_and_leaves_the_source_alone(self):
        before = (self.round_dir() / "results.json").read_text(encoding="utf-8")
        self.recalc()
        rd = self.round_dir(self.out)
        self.assertFalse((rd / "adj-args.json").exists())
        self.assertFalse((rd / "adj.json").exists())
        self.assertNotIn("r1-01:adjudicator", read(rd / "results.json"))
        rows = read(rd / "claude-usage.json")["workflows"]["wf_1"]
        self.assertEqual([r["job_id"] for r in rows], ["r1-01:a", "r1-01:b"])
        self.assertEqual(read(rd / "decisions.json")[0]["outcome"], "합의-사실")
        self.assertEqual((self.round_dir() / "results.json").read_text(encoding="utf-8"), before)
        self.assertTrue((self.round_dir() / "adj-args.json").is_file())

    def test_recalc_applies_lossy_like_judge(self):
        write(self.round_dir() / "lossy.json", {"r1-01": {"a": True, "b": False}})
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        row = json.loads((self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["on"]["outcome"], "판단불가")
        self.assertIn("UTF-8 아닌 패치", row["on"]["reasons"])
        self.assertTrue(read(self.out / "check.json")["ok"])

    def test_recalc_fails_without_lossy_record(self):
        (self.round_dir() / "lossy.json").unlink()
        code, text = self.recalc()
        self.assertEqual(code, 1)
        check = read(self.out / "check.json")
        self.assertFalse(check["ok"])
        self.assertIn("손실 여부 미확인", json.dumps(check["mismatches"], ensure_ascii=False))
        self.assertIn("x", text)

    def test_recalc_detects_branch_mismatch(self):
        (self.round_dir() / "adj-args.json").unlink()
        code, _ = self.recalc()
        self.assertEqual(code, 1)
        mismatch = read(self.out / "check.json")["mismatches"][0]
        self.assertEqual((mismatch["item"], mismatch["check"]), ("x", "branch"))

    def test_recalc_detects_token_mismatch(self):
        usage = read(self.round_dir() / "claude-usage.json")
        usage["workflows"]["wf_1"].append({**usage["workflows"]["wf_1"][2], "job_id": "r1-01:adjudicator-retry"})
        write(self.round_dir() / "claude-usage.json", usage)
        code, _ = self.recalc()
        self.assertEqual(code, 1)
        mismatch = read(self.out / "check.json")["mismatches"][0]
        self.assertEqual((mismatch["item"], mismatch["check"]), ("x", "tokens"))

    def test_recalc_copies_error_and_incomplete_items_unchanged_and_lists_them(self):
        (self.runs / "bad").mkdir()
        write(self.runs / "bad" / "meta.json", {"error": "boom"})
        (self.runs / "slow").mkdir()
        write(self.runs / "slow" / "meta.json", {"incomplete": True, "pending_run_dir": "/p"})
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertEqual(read(self.out / "bad" / "meta.json"), {"error": "boom"})
        self.assertTrue((self.out / "slow" / "meta.json").is_file())
        skipped = {row["item"]: row["reason"] for row in read(self.out / "check.json")["skipped"]}
        self.assertEqual(set(skipped), {"bad", "slow"})

    def test_recalc_keeps_items_the_rule_does_not_change(self):
        shutil.copytree(self.runs / "x", self.runs / "same")
        rd = self.round_dir(self.runs).parent.parent.parent / "same" / "run" / "round-1"
        results = read(rd / "results.json")
        results["r1-01:b"]["patch"] = results["r1-01:a"]["patch"]
        write(rd / "results.json", results)
        for name in ("adj-args.json", "adj.json"):
            (rd / name).unlink()
        ledger_path = self.runs / "same" / "ledger.json"
        ledger = read(ledger_path)
        ledger["rounds"][0]["decisions"][0].update(outcome="합의-사실", patch_from="a", reasons=[])
        write(ledger_path, ledger)
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertEqual(read(self.out / "check.json")["flipped"], 1)
        self.assertEqual(read(self.out / "same" / "ledger.json"), ledger)
        rows = [json.loads(line) for line in (self.out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({row["item"]: row["flipped"] for row in rows}, {"x": True, "same": False})

    def test_recalc_can_write_inside_the_runs_folder(self):
        self.out = self.runs / "adopt-recalc"
        code, _ = self.recalc()
        self.assertEqual(code, 0)
        self.assertFalse((self.out / "adopt-recalc").exists())
        self.assertTrue((self.out / "x" / "ledger.json").is_file())

    def test_recalc_refuses_a_non_empty_out_folder(self):
        self.out.mkdir()
        (self.out / "old.txt").write_text("x", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            self.recalc()
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
