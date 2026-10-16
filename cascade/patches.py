import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cascade.config import RepoConfig
from cascade.verify import run_format

INHERITED_GIT_ENV = ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE")
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]
STDERR_TAIL = 500
DROPPED_PREFIXES = ("index ", "similarity index ", "dissimilarity index ")
GIT_HEADER = "diff --git a/"
DIFF_FLAGS = ["--no-color", "--src-prefix=a/", "--dst-prefix=b/", "--no-renames", "--no-ext-diff", "--no-textconv", "--binary"]
HUNK_HEADER = re.compile(r"^(@@ -\S+ \+\S+ @@).*$")


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"applied": list(self.applied), "failed": [[fid, tail] for fid, tail in self.failed]}


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_GIT_ENV}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=False)


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _git_checked(cwd: Path, *args: str) -> str:
    p = _git(cwd, *args)
    if p.returncode != 0:
        raise RuntimeError(f"git 명령 실패 (git {' '.join(args)}): {_decode(p.stderr).strip()}")
    return _decode(p.stdout)


def extract(wt: Path, snap_commit: str, cfg: RepoConfig | None = None) -> str:
    wt = Path(wt)
    if cfg is not None:
        run_format(wt, cfg)
    _git_checked(wt, *NO_HOOKS, "add", "-A")
    return _git_checked(wt, "-c", "core.quotepath=false", "diff", "--cached", *DIFF_FLAGS, snap_commit)


def normalize(diff: str) -> str:
    lines = []
    for line in diff.split("\n"):
        if line.startswith(DROPPED_PREFIXES):
            continue
        line = line.rstrip(" \t")
        m = HUNK_HEADER.match(line)
        lines.append(m.group(1) if m else line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def same(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def _git_header_path(line: str) -> str | None:
    rest = line[len(GIT_HEADER):]
    size = (len(rest) - len(" b/")) // 2
    if size > 0 and rest[:size] == rest[size + len(" b/"):] and rest[size:size + 3] == " b/":
        return rest[:size]
    return None


def changed_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.split("\n"):
        if line.startswith(GIT_HEADER):
            path = _git_header_path(line.rstrip("\r"))
            if path is not None and path not in files:
                files.append(path)
    return files


def apply_all(root: Path, patches: list[tuple[str, str]]) -> ApplyReport:
    root = Path(root)
    report = ApplyReport()
    with tempfile.TemporaryDirectory() as tmp:
        for index, (finding_id, diff) in enumerate(patches):
            if not diff.strip():
                report.applied.append(finding_id)
                continue
            patch_file = Path(tmp) / f"{index}.patch"
            patch_file.write_bytes(diff.encode("utf-8"))
            check = _git(root, "apply", "--check", str(patch_file))
            if check.returncode == 0:
                done = _git(root, "apply", str(patch_file))
                if done.returncode == 0:
                    report.applied.append(finding_id)
                    continue
                check = done
            report.failed.append((finding_id, _decode(check.stderr)[-STDERR_TAIL:]))
    return report
