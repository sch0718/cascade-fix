import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import patches, snapshot  # noqa: E402
from cascade.config import RepoConfig  # noqa: E402
from helpers import git, make_repo  # noqa: E402

BASE = "a=0\nb=0\nx=0\nd=0\n"

P_LINE3_A = (
    "diff --git a/a.py b/a.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/a.py\n"
    "+++ b/a.py\n"
    "@@ -1,4 +1,4 @@\n"
    " a=0\n"
    " b=0\n"
    "-x=0\n"
    "+x=A\n"
    " d=0\n"
)
P_LINE3_B = P_LINE3_A.replace("x=A", "x=B").replace("2222222", "3333333")


class PatchesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()
        self.repo = make_repo(self.tmp / "repo", {"a.py": BASE})

    def test_same_ignores_index_lines_and_trailing_ws(self):
        other = P_LINE3_A.replace("1111111..2222222", "aaaaaaa..bbbbbbb").replace("+x=A\n", "+x=A  \n")
        self.assertNotEqual(P_LINE3_A, other)
        self.assertTrue(patches.same(P_LINE3_A, other))

    def test_different_hunks_not_same(self):
        self.assertFalse(patches.same(P_LINE3_A, P_LINE3_B))

    def test_normalize_strips_hunk_function_context(self):
        with_ctx = P_LINE3_A.replace("@@ -1,4 +1,4 @@\n", "@@ -1,4 +1,4 @@ def foo():\n")
        norm = patches.normalize(with_ctx)
        self.assertIn("@@ -1,4 +1,4 @@\n", norm)
        self.assertNotIn("def foo", norm)
        self.assertNotIn("index ", norm)
        self.assertTrue(patches.same(with_ctx, P_LINE3_A))
        self.assertTrue(norm.endswith("\n") and not norm.endswith("\n\n"))

    def test_changed_files_lists_added_and_deleted(self):
        diff = (
            P_LINE3_A
            + "diff --git a/new.txt b/new.txt\nnew file mode 100644\nindex 0000000..e69de29\n"
            "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+n\n"
            + "diff --git a/old.txt b/old.txt\ndeleted file mode 100644\nindex e69de29..0000000\n"
            "--- a/old.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-o\n"
            + P_LINE3_A
        )
        self.assertEqual(patches.changed_files(diff), ["a.py", "new.txt", "old.txt"])

    def test_apply_conflict_goes_to_undecidable(self):
        rep = patches.apply_all(self.repo, [("r1-01", P_LINE3_A), ("r1-02", P_LINE3_B)])
        self.assertEqual(rep.applied, ["r1-01"])
        self.assertEqual(rep.failed[0][0], "r1-02")
        self.assertTrue(rep.failed[0][1])
        self.assertIn("x=A", (self.repo / "a.py").read_text())

    def test_apply_all_does_not_touch_index(self):
        rep = patches.apply_all(self.repo, [("r1-01", P_LINE3_A), ("r1-00", "")])
        self.assertEqual(rep.applied, ["r1-01", "r1-00"])
        self.assertEqual(git(self.repo, "diff", "--cached"), "")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "M a.py")

    def test_apply_report_roundtrip_dict(self):
        rep = patches.ApplyReport(applied=["r1-01"], failed=[("r1-02", "err")])
        self.assertEqual(rep.to_dict(), {"applied": ["r1-01"], "failed": [["r1-02", "err"]]})

    def test_extract_runs_format_then_diffs_against_snapshot(self):
        snap = snapshot.create_snapshot(self.repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "a.py").write_text("a=0\nb=0\nx=1\nd=0\n")
        (wt / "fmt_me.py").write_text("y=2\n")
        snapshot.commit_worktree(wt, "worker change")
        cfg = RepoConfig(format=['python3 -c "import pathlib; pathlib.Path(\'fmt_me.py\').write_text(\'y = 2\\n\')"'])
        diff = patches.extract(wt, snap.commit, cfg)
        self.assertIn("+x=1", diff)
        self.assertIn("+y = 2", diff)
        self.assertNotIn("+y=2", diff)
        self.assertEqual(patches.changed_files(diff), ["a.py", "fmt_me.py"])
        without = patches.extract(wt, snap.commit)
        self.assertIn("+y = 2", without)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
