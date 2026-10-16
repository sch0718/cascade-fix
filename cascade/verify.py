import os
import re
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from cascade.config import RepoConfig
from cascade.findings import Finding

TIMEOUT = 600
TAIL_CHARS = 2000
REPRO_TAIL_CHARS = 500
TIMEOUT_TAIL = "시간 초과(600초)"
SKIP_DIRS = {"node_modules", ".git", ".cascade-fix"}
TEST_DIRS = {"tests", "test"}
TEST_NAME = re.compile(r"^(?:test_.+\.py|.+_test\.py|.+\.test\..+|.+\.spec\..+)$")
JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
DOC_EXTENSIONS = (".py", ".js", ".ts", ".md", ".toml", ".json", ".yaml", ".yml", ".sh", ".css", ".html")
EXTERNAL_PREFIX = "외부/"
FENCE = re.compile(r"^[ \t]*```([^\n`]*)\n(.*?)^[ \t]*```[ \t]*$", re.M | re.S)
URL = re.compile(r"https?://\S+")
PATH_LINE = re.compile(r"([\w./-]+):\d+(?:-\d+)?")
BACKTICK = re.compile(r"`([^`\s]+)`")
LINE_SUFFIX = re.compile(r":\d+(?:-\d+)?$")


@dataclass
class VerifyResult:
    cmd: str
    ok: bool
    tail: str

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: str, cwd: Path) -> tuple[int, str, bool]:
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return -1, "", True
    return proc.returncode, (out or "") + (err or ""), False


def _result(cmd: str, cwd: Path) -> VerifyResult:
    rc, output, timed_out = _run(cmd, cwd)
    if timed_out:
        return VerifyResult(cmd=cmd, ok=False, tail=TIMEOUT_TAIL)
    return VerifyResult(cmd=cmd, ok=rc == 0, tail=output[-TAIL_CHARS:])


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _python_modules(rel: Path) -> list[str]:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


def _python_import_pattern(rel: Path) -> re.Pattern | None:
    modules = _python_modules(rel)
    if not modules:
        return None
    alt = "|".join(re.escape(m) for m in modules)
    return re.compile(
        rf"^[ \t]*(?:import\s+(?:[\w.]+\s*,\s*)*(?:{alt})\b|from\s+(?:{alt})\s+import\b)", re.M
    )


def _js_import_pattern(rel: Path) -> re.Pattern:
    stem = re.escape(rel.stem)
    return re.compile(
        rf"""(?:from\s+|require\(\s*|import\s*\(\s*|import\s+)['"][^'"\n]*/{stem}(?:\.\w+)?['"]"""
    )


def related_tests(root: Path, changed: list[str]) -> list[str]:
    root = Path(root)
    all_tests = [p for p in _iter_files(root) if TEST_NAME.match(p.name)]
    found: set[str] = set()
    for rel_str in changed:
        rel = Path(rel_str)
        stem = rel.stem
        name_matches = {f"test_{stem}", f"{stem}_test", f"{stem}.test"}
        import_pattern = None
        if rel.suffix == ".py":
            import_pattern = _python_import_pattern(rel)
        elif rel.suffix in JS_SUFFIXES:
            import_pattern = _js_import_pattern(rel)
        for test in all_tests:
            test_rel = test.relative_to(root)
            if test_rel.parent == rel.parent:
                found.add(test_rel.as_posix())
            elif TEST_DIRS & set(test_rel.parts[:-1]) and test.stem in name_matches:
                found.add(test_rel.as_posix())
            elif import_pattern is not None:
                try:
                    text = test.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if import_pattern.search(text):
                    found.add(test_rel.as_posix())
    return sorted(found)


def run_verifiers(
    wt: Path, cfg: RepoConfig, changed: list[str], stage: Literal["worker", "final"]
) -> list[VerifyResult]:
    wt = Path(wt)
    if stage == "final":
        commands = list(cfg.full_tests)
    else:
        files = " ".join(related_tests(wt, changed))
        commands = [c.replace("{files}", files) for c in cfg.tests]
        commands += [*cfg.build, *cfg.typecheck, *cfg.lint, *cfg.format]
    return [_result(cmd, wt) for cmd in commands]


def run_format(wt: Path, cfg: RepoConfig) -> None:
    for cmd in cfg.format:
        _run(cmd, Path(wt))


def _strip_fences(text: str) -> str:
    return FENCE.sub("", text)


def _looks_like_path(token: str) -> bool:
    return "/" in token or token.endswith(DOC_EXTENSIONS)


def check_doc_refs(wt: Path, doc: Path) -> list[str]:
    wt = Path(wt)
    text = URL.sub("", _strip_fences(Path(doc).read_text(encoding="utf-8")))
    candidates = [m.group(1) for m in PATH_LINE.finditer(text)]
    candidates += [LINE_SUFFIX.sub("", m.group(1)).rstrip(".,;)") for m in BACKTICK.finditer(text)]
    missing = set()
    for path in candidates:
        if not path or not _looks_like_path(path):
            continue
        if path.startswith(("http", EXTERNAL_PREFIX)) or "*" in path or "{" in path:
            continue
        if not (wt / path).exists():
            missing.add(path)
    return sorted(missing)


def check_snippets(wt: Path, doc: Path, cfg: RepoConfig) -> list[VerifyResult]:
    wt = Path(wt)
    text = Path(doc).read_text(encoding="utf-8")
    results = []
    for n, match in enumerate(FENCE.finditer(text)):
        info = match.group(1).strip().split()
        lang = info[0] if info else ""
        command = cfg.doc_snippet_check.get(lang) or cfg.doc_snippet_check.get(lang.lower())
        if not lang or command is None:
            continue
        rel = f".cascade-fix/snippets/{n}.{lang}"
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(match.group(2), encoding="utf-8")
        result = _result(command.replace("{file}", rel), wt)
        result.cmd = f"[블록 {n}] {result.cmd}"
        results.append(result)
    return results


def _confirmed_paragraph(body: str) -> str | None:
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("**확인한 내용")), None)
    if start is None:
        return None
    collected = []
    in_fence = False
    for line in lines[start:]:
        stripped = line.strip()
        if not in_fence and collected:
            if not stripped or stripped.startswith("**근거"):
                break
        if stripped.startswith("```"):
            in_fence = not in_fence
        collected.append(line)
    return "\n".join(collected)


def repro_cmds(finding: Finding) -> list[str]:
    paragraph = _confirmed_paragraph(finding.body)
    if paragraph is None:
        return []
    cmds = []
    for match in FENCE.finditer(paragraph):
        for line in match.group(2).splitlines():
            line = line.strip()
            if line.startswith("$ "):
                line = line[2:].strip()
            if line:
                cmds.append(line)
    return cmds


def run_repro(base_wt: Path, fix_wt: Path, cmds: list[str]) -> list[VerifyResult]:
    results = []
    for cmd in cmds:
        base_rc, _, base_timeout = _run(cmd, Path(base_wt))
        fix_rc, fix_out, fix_timeout = _run(cmd, Path(fix_wt))
        base_failed = base_timeout or base_rc != 0
        fix_passed = not fix_timeout and fix_rc == 0
        base_label = "시간 초과" if base_timeout else base_rc
        fix_label = "시간 초과" if fix_timeout else fix_rc
        tail = f"수정 전 rc={base_label} / 수정 후 rc={fix_label}\n{fix_out[-REPRO_TAIL_CHARS:]}"
        results.append(VerifyResult(cmd=cmd, ok=base_failed and fix_passed, tail=tail))
    return results
