import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RUN_DIR_NAME = ".cascade-fix"
EXCLUDE_LINE = f"{RUN_DIR_NAME}/"
EXTERNAL_PREFIX = "외부"
COMMIT_IDENTITY = ["-c", "user.name=cascade-fix", "-c", "user.email=cascade-fix@local", "-c", "commit.gpgsign=false"]


@dataclass
class Snapshot:
    root: Path
    run_id: str
    branch: str
    commit: str
    run_dir: Path
    worktrees: dict[str, Path] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "run_id": self.run_id,
            "branch": self.branch,
            "commit": self.commit,
            "run_dir": str(self.run_dir),
            "worktrees": {name: str(path) for name, path in self.worktrees.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        return cls(
            root=Path(data["root"]),
            run_id=data["run_id"],
            branch=data["branch"],
            commit=data["commit"],
            run_dir=Path(data["run_dir"]),
            worktrees={name: Path(path) for name, path in data.get("worktrees", {}).items()},
        )


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git 명령 실패 (git {' '.join(args)}): {e.stderr.strip()}") from e
    return p.stdout.strip()


def ensure_excluded(root: Path) -> None:
    root = Path(root)
    exclude = Path(_git(root, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = root / exclude
    text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if EXCLUDE_LINE in text.splitlines():
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    prefix = "" if text == "" or text.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as f:
        f.write(f"{prefix}{EXCLUDE_LINE}\n")


def create_snapshot(root: Path, run_id: str, extra_files: list[Path]) -> Snapshot:
    root = Path(root).resolve()
    try:
        _git(root, "rev-parse", "--verify", "-q", "HEAD")
    except RuntimeError:
        raise RuntimeError("커밋이 없는 저장소") from None
    names = [Path(p).name for p in extra_files]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise RuntimeError(f"저장소 밖 문서의 파일명이 겹친다: {', '.join(duplicated)}")

    ensure_excluded(root)
    run_dir = root / RUN_DIR_NAME / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(run_dir / "index.tmp")}
    _git(root, "read-tree", "HEAD", env=env)
    _git(root, "add", "-A", env=env)

    if extra_files:
        external = run_dir / "external"
        external.mkdir(exist_ok=True)
        for src in extra_files:
            copy = external / Path(src).name
            shutil.copyfile(src, copy)
            blob = _git(root, "hash-object", "-w", str(copy))
            _git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{EXTERNAL_PREFIX}/{copy.name}", env=env)

    tree = _git(root, "write-tree", env=env)
    commit = _git(root, *COMMIT_IDENTITY, "commit-tree", tree, "-p", "HEAD", "-m", f"cascade-fix 스냅샷 {run_id}")
    branch = f"cascade-fix/{run_id}"
    _git(root, "branch", branch, commit)
    return Snapshot(root=root, run_id=run_id, branch=branch, commit=commit, run_dir=run_dir)


def add_worktree(snap: Snapshot, name: str) -> Path:
    path = snap.run_dir / "wt" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(snap.root, "worktree", "add", "--detach", str(path), snap.commit)
    snap.worktrees[name] = path
    return path


def commit_worktree(wt: Path, message: str) -> str | None:
    wt = Path(wt)
    _git(wt, "add", "-A")
    if _git(wt, "status", "--porcelain") == "":
        return None
    _git(wt, *COMMIT_IDENTITY, "commit", "-q", "-m", message)
    return _git(wt, "rev-parse", "HEAD")


def cleanup(snap: Snapshot, keep_run_dir: bool) -> None:
    for path in snap.worktrees.values():
        if path.exists():
            _git(snap.root, "worktree", "remove", "--force", str(path))
    _git(snap.root, "worktree", "prune")
    snap.worktrees.clear()
    if _git(snap.root, "branch", "--list", snap.branch):
        _git(snap.root, "branch", "-D", snap.branch)
    if not keep_run_dir:
        shutil.rmtree(snap.run_dir, ignore_errors=True)
        return
    shutil.rmtree(snap.run_dir / "wt", ignore_errors=True)
    (snap.run_dir / "index.tmp").unlink(missing_ok=True)
