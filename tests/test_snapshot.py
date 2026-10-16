import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import snapshot  # noqa: E402
from helpers import git, make_repo  # noqa: E402


def show(repo, spec):
    return subprocess.run(["git", "show", spec], cwd=repo, capture_output=True, text=True, check=True).stdout


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_snapshot_keeps_user_index(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        (repo / "a.py").write_text("x=2\n")
        (repo / "new.txt").write_text("n\n")
        ext = self.tmp / "outside" / "spec.md"
        ext.parent.mkdir()
        ext.write_text("# s\n")
        before = git(repo, "status", "--porcelain")
        head_before = git(repo, "rev-parse", "HEAD")
        snap = snapshot.create_snapshot(repo, "run1", [ext])
        self.assertEqual(git(repo, "status", "--porcelain"), before)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")
        tree = git(repo, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", snap.commit).splitlines()
        self.assertIn("new.txt", tree)
        self.assertIn("외부/spec.md", tree)
        self.assertEqual(show(repo, f"{snap.commit}:a.py"), "x=2\n")
        self.assertEqual(show(repo, f"{snap.commit}:외부/spec.md"), "# s\n")
        self.assertEqual(snap.branch, "cascade-fix/run1")
        self.assertEqual(git(repo, "rev-parse", snap.branch), snap.commit)
        self.assertEqual(git(repo, "rev-parse", f"{snap.commit}^"), head_before)
        self.assertEqual(snap.run_dir, repo / ".cascade-fix" / "run1")

    def test_snapshot_excludes_run_dir(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        stale = repo / ".cascade-fix" / "old" / "junk.txt"
        stale.parent.mkdir(parents=True)
        stale.write_text("j\n")
        snap = snapshot.create_snapshot(repo, "run1", [])
        self.assertTrue((snap.run_dir / "index.tmp").exists())
        tree = git(repo, "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", snap.commit).splitlines()
        self.assertEqual(tree, ["a.py"])
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_snapshot_without_commits_fails(self):
        repo = self.tmp / "empty"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        with self.assertRaisesRegex(RuntimeError, "커밋이 없는 저장소"):
            snapshot.create_snapshot(repo, "run1", [])

    def test_worktree_from_snapshot_and_commit(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        (repo / "a.py").write_text("x=2\n")
        before = git(repo, "status", "--porcelain")
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        self.assertEqual(wt, snap.run_dir / "wt" / "w1")
        self.assertEqual(snap.worktrees["w1"], wt)
        self.assertEqual((wt / "a.py").read_text(), "x=2\n")
        self.assertIsNone(snapshot.commit_worktree(wt, "변경 없음"))
        (wt / "a.py").write_text("x=3\n")
        sha = snapshot.commit_worktree(wt, "a.py 고침")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), sha)
        self.assertEqual(git(wt, "rev-parse", f"{sha}^"), snap.commit)
        self.assertEqual(git(wt, "log", "-1", "--format=%s"), "a.py 고침")
        self.assertEqual(git(wt, "status", "--porcelain"), "")
        base = snapshot.add_worktree(snap, "base")
        self.assertEqual((base / "a.py").read_text(), "x=2\n")
        self.assertEqual(git(repo, "status", "--porcelain"), before)
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual((repo / "a.py").read_text(), "x=2\n")

    def test_commit_worktree_skips_hooks(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        (wt / "a.py").write_text("x=3\n")
        sha = snapshot.commit_worktree(wt, "훅 무시")
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(git(wt, "rev-parse", f"{sha}^"), snap.commit)

    def test_cleanup_removes_branch_and_worktrees(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        wt = snapshot.add_worktree(snap, "w1")
        snapshot.add_worktree(snap, "base")
        (snap.run_dir / "ledger.json").write_text("{}")
        snapshot.cleanup(snap, keep_run_dir=True)
        self.assertFalse(wt.exists())
        self.assertFalse((snap.run_dir / "wt").exists())
        self.assertFalse((snap.run_dir / "index.tmp").exists())
        self.assertTrue((snap.run_dir / "ledger.json").exists())
        self.assertEqual(git(repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) - 1, 1)
        snap2 = snapshot.create_snapshot(repo, "run2", [])
        snapshot.add_worktree(snap2, "w1")
        snapshot.cleanup(snap2, keep_run_dir=False)
        self.assertFalse(snap2.run_dir.exists())
        self.assertEqual(git(repo, "branch", "--list", "cascade-fix/*"), "")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_ensure_excluded_adds_once(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snapshot.ensure_excluded(repo)
        snapshot.ensure_excluded(repo)
        lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
        self.assertEqual(lines.count(".cascade-fix/"), 1)
        (repo / ".cascade-fix").mkdir()
        (repo / ".cascade-fix" / "x").write_text("x")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

    def test_snapshot_roundtrip_dict(self):
        repo = make_repo(self.tmp / "repo", {"a.py": "x=1\n"})
        snap = snapshot.create_snapshot(repo, "run1", [])
        snapshot.add_worktree(snap, "base")
        data = snap.to_dict()
        self.assertEqual(data["root"], str(repo))
        self.assertEqual(data["worktrees"], {"base": str(snap.run_dir / "wt" / "base")})
        self.assertEqual(snapshot.Snapshot.from_dict(data), snap)


if __name__ == "__main__":
    unittest.main()
