import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cascade.config import RepoConfig
from cascade.runners import (
    RUNNER_CODEX,
    RUNNER_CURSOR,
    STATUS_OK,
    CursorRunner,
    _execute,
    _Run,
    _tail,
    cache_env,
    extract_json_block,
    find_codex,
    find_cursor_agent,
    restore_tree,
    tree_of,
)
from cascade.verify import FILES, TIMEOUT, run_commands
from cascade.workers import DEFAULT_CURSOR_MODEL, demote

INCONCLUSIVE = -1
PROBE = "true"
CODEX_PROBE = ":"
_CODEX_SANDBOX_MODE = 'sandbox_mode="workspace-write"'
_CURSOR_PROMPT = (
    "아래 셸 명령을 작업 디렉터리에서 하나씩 그대로 실행하고 각 종료 코드만 보고하라. "
    "파일을 고치거나 다른 명령을 돌리지 않는다. 실행하지 못한 명령은 exit에 -1을 쓴다. "
    "마지막에 {{\"results\":[{{\"cmd\":\"<명령>\",\"exit\":<종료 코드>}}]}} 형식의 JSON 객체 하나만 ```json 블록으로 낸다.\n\n"
    "명령 목록:\n{commands}\n"
)
Check = Callable[[Path, list[str]], dict[str, int]]


class ProbeFailed(Exception):
    """저장소 명령 이전에 돌린 점검 명령이 실패했다. 샌드박스 탓이 아니라 점검 자체를 못 한 것이다."""


def check_file() -> Path:
    if os.environ.get("CASCADE_SANDBOX_CHECK"):
        return Path(os.environ["CASCADE_SANDBOX_CHECK"])
    return Path.home() / ".claude" / "cascade-fix" / "sandbox-check.json"


def commands(cfg: RepoConfig) -> list[str]:
    return list(dict.fromkeys([*cfg.tests, *cfg.build, *cfg.typecheck, *cfg.lint]))


def cache_key(root: Path, runner: str, cmds: list[str]) -> str:
    digest = hashlib.sha256(json.dumps(cmds, ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"{root}|{runner}|{digest}"


def _runnable(template: str) -> str:
    return template.replace(FILES, "").rstrip()


def _codex_sandbox(exe: str, wt: Path, command: str) -> _Run:
    tmp = Path(tempfile.mkdtemp(prefix="cascade-cache-"))
    try:
        home = tmp / "codex-home"
        home.mkdir()
        argv = [exe, "sandbox", "-C", str(wt), "-c", _CODEX_SANDBOX_MODE, "--", "sh", "-c", command]
        env = {**os.environ, **cache_env(tmp), "CODEX_HOME": str(home)}
        return _execute(argv, "", Path(wt), env, TIMEOUT)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _probe_detail(run: _Run) -> str:
    if run.missing:
        return f"실행하지 못했다: {_tail(run.stderr)}"
    if run.timed_out:
        return f"시간 초과({TIMEOUT}초)"
    return f"종료 코드 {run.returncode}: {_tail(run.stderr)}" if run.stderr.strip() else f"종료 코드 {run.returncode}"


def run_codex(exe: str | None, wt: Path, cmds: list[str]) -> dict[str, int]:
    if exe is None:
        return {cmd: INCONCLUSIVE for cmd in cmds}
    probe = _codex_sandbox(exe, wt, CODEX_PROBE)
    if probe.timed_out or probe.missing or probe.returncode != 0:
        raise ProbeFailed(_probe_detail(probe))
    results = {}
    for cmd in cmds:
        run = _codex_sandbox(exe, wt, _runnable(cmd))
        results[cmd] = INCONCLUSIVE if run.timed_out or run.missing else run.returncode
    return results


def _cursor_exit(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else INCONCLUSIVE


def run_cursor(runner: CursorRunner, wt: Path, cmds: list[str], model: str = DEFAULT_CURSOR_MODEL) -> dict[str, int]:
    expanded = {cmd: _runnable(cmd) for cmd in cmds}
    prompt = _CURSOR_PROMPT.format(commands=json.dumps([PROBE, *expanded.values()], ensure_ascii=False, indent=2))
    outcome = runner.ask(wt, prompt, model)
    block = extract_json_block(outcome.output["text"]) if outcome.status == STATUS_OK else None
    reported = block.get("results") if block else None
    items = [item for item in reported if isinstance(item, dict) and isinstance(item.get("cmd"), str)] \
        if isinstance(reported, list) else []
    probe_at = next((i for i, item in enumerate(items) if item["cmd"] == PROBE), None)
    if outcome.status != STATUS_OK:
        raise ProbeFailed(f"Cursor 호출이 {outcome.status}: {outcome.detail}")
    if probe_at is None:
        raise ProbeFailed(f"점검용 첫 명령 {PROBE}의 결과가 보고되지 않았다")
    probe_exit = _cursor_exit(items[probe_at].get("exit"))
    if probe_exit != 0:
        raise ProbeFailed(f"점검용 첫 명령 {PROBE}이 종료 코드 {probe_exit}로 보고됐다")
    exits = {item["cmd"]: _cursor_exit(item.get("exit")) for i, item in enumerate(items) if i != probe_at}
    return {cmd: exits.get(run, INCONCLUSIVE) for cmd, run in expanded.items()}


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _default_checks(workers: dict) -> dict[str, Check]:
    checks: dict[str, Check] = {RUNNER_CODEX: lambda wt, cmds: run_codex(find_codex(), wt, cmds)}
    model = next((spec["model"] for spec in workers.values() if spec["runner"] == RUNNER_CURSOR), DEFAULT_CURSOR_MODEL)

    def cursor(wt: Path, cmds: list[str]) -> dict[str, int]:
        exe = find_cursor_agent()
        return run_cursor(CursorRunner(exe), wt, cmds, model) if exe else {cmd: INCONCLUSIVE for cmd in cmds}

    checks[RUNNER_CURSOR] = cursor
    return checks


def ensure(root: Path, cfg: RepoConfig, base_wt: Path, workers: dict, baseline_failed: list[str],
           runners: dict[str, Check] | None = None) -> tuple[dict, list[str]]:
    cmds = [c for c in commands(cfg) if c not in baseline_failed]
    used = [r for r in (RUNNER_CODEX, RUNNER_CURSOR) if any(spec["runner"] == r for spec in workers.values())]
    if not cmds or not used:
        return workers, []
    checks = runners or _default_checks(workers)
    path = check_file()
    saved = _load(path)
    outside: dict[str, bool] = {}

    def fails_outside(pending: list[str]) -> set[str]:
        todo = [cmd for cmd in pending if cmd not in outside]
        for cmd, result in zip(todo, run_commands(Path(base_wt), [_runnable(c) for c in todo])):
            outside[cmd] = not result.ok
        return {cmd for cmd in pending if outside[cmd]}

    lines = []
    before = tree_of(base_wt)
    try:
        for runner in used:
            key = cache_key(root, runner, cmds)
            entry = saved.get(key)
            probe_failure = ""
            if not isinstance(entry, dict):
                try:
                    exits = checks[runner](Path(base_wt), cmds)
                except ProbeFailed as e:
                    exits, probe_failure = {}, str(e)
                failed = [cmd for cmd in cmds if exits.get(cmd, INCONCLUSIVE) != 0]
                real = [cmd for cmd in failed if exits.get(cmd, INCONCLUSIVE) != INCONCLUSIVE]
                not_sandbox = fails_outside(real) if real else set()
                failed = [cmd for cmd in failed if cmd not in not_sandbox]
                entry = {"ok": not failed, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "failed": failed}
                if not any(exits.get(cmd, INCONCLUSIVE) == INCONCLUSIVE for cmd in cmds):
                    saved[key] = entry
                    _save(path, saved)
            if probe_failure:
                workers = demote(workers, runner)
                lines.append(f"{runner} 샌드박스 점검 명령 자체가 실패했다({probe_failure}). 검증 명령 탓이 아니라 점검을 못 한 것이라 "
                             "결과를 저장하지 않고, 이 저장소에서는 Claude 작업자를 쓴다")
            elif not entry.get("ok"):
                workers = demote(workers, runner)
                lines.append(f"{runner} 샌드박스에서 검증 명령이 실패해 이 저장소에서는 Claude 작업자를 쓴다: "
                             f"{', '.join(entry.get('failed', []))}")
    finally:
        restore_tree(base_wt, before)
    return workers, lines
