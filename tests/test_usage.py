import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cascade import usage  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "workflow-run"


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


class UsageTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def make_run(self, usage_row, model="claude-sonnet-5-5"):
        run = self.tmp / "run"
        run.mkdir()
        write_jsonl(run / "journal.jsonl", [{"type": "started", "agentId": "a1", "label": "x"}])
        (run / "agent-a1.meta.json").write_text(json.dumps({"model": model}), encoding="utf-8")
        write_jsonl(run / "agent-a1.jsonl", [{"message": {"id": "m1", "usage": usage_row}}])
        return run

    def test_reads_usage_per_label(self):
        got = {u.label: u for u in usage.read_workflow(FIXTURE)}
        a = got["r1-01 a"]
        self.assertEqual((a.label, a.model, a.input, a.output, a.cache_read, a.cache_write),
                         ("r1-01 a", "claude-sonnet-5-5", 4, 80, 2000, 150))
        self.assertEqual(got["r1-01 b"].model, "claude-haiku-5-5")

    def test_duplicate_message_counted_once(self):
        self.assertEqual({u.label: u.output for u in usage.read_workflow(FIXTURE)}["r1-01 a"], 80)

    def test_cost_sums_per_message(self):
        a = {u.label: u for u in usage.read_workflow(FIXTURE)}["r1-01 a"]
        want = (usage.cost("claude-sonnet-5-5", 2, 30, 1000, 100)
                + usage.cost("claude-sonnet-5-5", 2, 50, 1000, 50))
        self.assertAlmostEqual(a.cost, want)

    def test_cost_is_none_when_model_unknown(self):
        run = self.make_run({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0}, model="gpt-6.1-sol")
        self.assertIsNone(usage.read_workflow(run)[0].cost)

    def test_missing_files_raise_format_error(self):
        with self.assertRaises(usage.UsageFormatError):
            usage.read_workflow(self.tmp)

    def test_missing_usage_key_raises_format_error(self):
        run = self.make_run({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0})
        with self.assertRaises(usage.UsageFormatError) as cm:
            usage.read_workflow(run)
        self.assertIn("cache_creation_input_tokens", str(cm.exception))

    def test_missing_agent_file_raises_format_error(self):
        run = self.make_run({"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0})
        (run / "agent-a1.jsonl").unlink()
        with self.assertRaises(usage.UsageFormatError):
            usage.read_workflow(run)

    def test_find_by_run_id(self):
        root = self.tmp / "projects"
        d = root / "p" / "s" / "subagents" / "workflows" / "wf_x"
        d.mkdir(parents=True)
        self.assertEqual(usage.find_workflow_dir("wf_x", root), d)
        with self.assertRaises(usage.UsageFormatError):
            usage.find_workflow_dir("wf_none", root)

    def test_find_by_run_id_ambiguous_raises(self):
        root = self.tmp / "projects"
        for s in ("s1", "s2"):
            (root / "p" / s / "subagents" / "workflows" / "wf_x").mkdir(parents=True)
        with self.assertRaises(usage.UsageFormatError):
            usage.find_workflow_dir("wf_x", root)

    def test_find_by_directory(self):
        self.assertEqual(usage.find_workflow_dir(str(FIXTURE), self.tmp / "none"), FIXTURE)

    def test_cost(self):
        self.assertAlmostEqual(usage.cost("claude-opus-5-5", 1_000_000, 1_000_000, 0, 0), 24.0)
        self.assertIsNone(usage.cost("gpt-6.1-sol", 1, 1, 0, 0))

    def test_cost_cache_prices(self):
        self.assertAlmostEqual(usage.cost("claude-opus-5-5", 0, 0, 1_000_000, 1_000_000), 0.20 + 5.0)
        self.assertAlmostEqual(usage.cost("claude-fable-5-1", 0, 0, 1_000_000, 0), 0.25)

    def test_haiku_tier_is_per_request(self):
        two = 2 * usage.cost("claude-haiku-5-5", 60_000, 0, 0, 0)
        one = usage.cost("claude-haiku-5-5", 120_000, 0, 0, 0)
        self.assertAlmostEqual(two, 120_000 * 0.10 / 1e6)
        self.assertAlmostEqual(one, 120_000 * 0.50 / 1e6)

    def test_haiku_tier_counts_cache_in_prompt_size(self):
        low = usage.cost("claude-haiku-5-5", 1, 0, 99_000, 0)
        high = usage.cost("claude-haiku-5-5", 1, 0, 100_000, 0)
        self.assertAlmostEqual(low, 1 * 0.10 / 1e6 + 99_000 * 0.01 / 1e6)
        self.assertAlmostEqual(high, 1 * 0.50 / 1e6 + 100_000 * 0.05 / 1e6)

    def test_unconfirmed_price_key_gives_none(self):
        saved = usage.PRICES["claude-sonnet-5-5"].pop("cache_write")
        self.addCleanup(usage.PRICES["claude-sonnet-5-5"].__setitem__, "cache_write", saved)
        self.assertIsNone(usage.cost("claude-sonnet-5-5", 1, 1, 0, 1))
        self.assertIsNotNone(usage.cost("claude-sonnet-5-5", 1, 1, 0, 0))


if __name__ == "__main__":
    unittest.main()
