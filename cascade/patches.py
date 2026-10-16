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


def _sections(diff: str) -> list[tuple[str | None, str]]:
    sections: list[tuple[str | None, list[str]]] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith(GIT_HEADER) or not sections:
            path = _git_header_path(line.rstrip("\r\n")) if line.startswith(GIT_HEADER) else None
            sections.append((path, []))
        sections[-1][1].append(line)
    return [(path, "".join(lines)) for path, lines in sections]


def _apply_outside(work: Path, rel: str, original: Path, text: str) -> tuple[bytes | None, str | None]:
    target = work / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if original.is_file():
        target.write_bytes(original.read_bytes())
    patch_file = work / "section.patch"
    patch_file.write_bytes(text.encode("utf-8"))
    env = {k: v for k, v in os.environ.items() if k not in INHERITED_GIT_ENV}
    env["GIT_CEILING_DIRECTORIES"] = str(work.parent)
    p = subprocess.run(["git", "apply", str(patch_file)], cwd=work, env=env, capture_output=True, check=False)
    if p.returncode != 0:
        return None, _decode(p.stderr)[-STDERR_TAIL:]
    return (target.read_bytes() if target.exists() else None), None


def apply_all(root: Path, patches: list[tuple[str, str]], external: dict[str, Path] | None = None) -> ApplyReport:
    root = Path(root)
    external = {k: Path(v) for k, v in (external or {}).items()}
    report = ApplyReport()
    with tempfile.TemporaryDirectory() as tmp:
        for index, (finding_id, diff) in enumerate(patches):
            if not diff.strip():
                report.applied.append(finding_id)
                continue
            sections = _sections(diff)
            inner = "".join(text for path, text in sections if path not in external)
            outside: dict[str, bytes | None] = {}
            error = None
            for n, (path, text) in enumerate(sections):
                if path in external:
                    content, error = _apply_outside(Path(tmp) / f"{index}-{n}", path, external[path], text)
                    if error is not None:
                        break
                    outside[path] = content
            if error is None and inner.strip():
                patch_file = Path(tmp) / f"{index}.patch"
                patch_file.write_bytes(inner.encode("utf-8"))
                check = _git(root, "apply", "--check", str(patch_file))
                if check.returncode == 0:
                    check = _git(root, "apply", str(patch_file))
                if check.returncode != 0:
                    error = _decode(check.stderr)[-STDERR_TAIL:]
            if error is not None:
                report.failed.append((finding_id, error))
                continue
            for path, content in outside.items():
                if content is None:
                    external[path].unlink(missing_ok=True)
                else:
                    external[path].write_bytes(content)
            report.applied.append(finding_id)
    return report
