import ctypes
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cascade.jobs import SCHEMAS, valid_output
from cascade.patches import NO_HOOKS, _git_checked

STATUS_OK, STATUS_INVALID, STATUS_LIMIT, STATUS_AUTH, STATUS_TIMEOUT, STATUS_ERROR, STATUS_SKIPPED = (
    "ok", "invalid", "limit", "auth", "timeout", "error", "skipped")
RUNNER_CODEX, RUNNER_CURSOR = "codex", "cursor"
JOB_TIMEOUT = 900
MAX_PARALLEL = 4
STATUS_COMMAND_TIMEOUT = 30
KILL_GRACE = 5
CODEX_WARMUP_AGE = timedelta(days=7)
DETAIL_TAIL = 500

_CODEX_APP_PATHS = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
)
_CODEX_EXTENSION_GLOB = ".cursor/extensions/openai.chatgpt-*/bin/macos-aarch64/codex"
_LIMIT_PATTERN = re.compile(r"usage limit|rate limit|quota|too many requests|\b429\b|hit your .{0,30}limit", re.IGNORECASE)
_AUTH_PATTERN = re.compile(
    r"authentication required|not logged in|unauthorized|\b401\b|refresh token|login required|codex login|cursor-agent login",
    re.IGNORECASE)
_FENCED_JSON = re.compile(r"```json[ \t]*\r?\n(.*?)```", re.DOTALL)

_SENSITIVE_DIRS = (".ssh", ".codex", ".cursor", ".claude", ".aws", ".config", ".gnupg", "Library/Keychains")
_PROFILE_DENY = {
    "review": ["Shell(*)", "Write(**)", "WebFetch(*)", "Mcp(*:*)"],
    "worker": ["WebFetch(*)", "Mcp(*:*)"],
    "readonly": ["Write(**)", "WebFetch(*)", "Mcp(*:*)"],
}
_TOOL_ROOTS = {"PYENV_ROOT": ".pyenv", "UV_PYTHON_INSTALL_DIR": ".local/share/uv/python"}

_OUTPUT_INSTRUCTION = (
    "\n\n## 출력 형식\n\n"
    "마지막에 아래 JSON 스키마를 따르는 객체 하나만 ```json 블록으로 낸다. "
    "model_id에는 아무 값이나 넣어도 된다(실행기가 덮어쓴다).\n\n{schema}\n"
)
_RETRY_PROMPT = (
    "방금 답의 결과를 아래 JSON 스키마를 따르는 JSON 객체 하나로만 다시 내라. "
    "```json 블록 하나만 쓰고 다른 글은 쓰지 않는다. model_id에는 아무 값이나 넣어도 된다.\n\n{schema}\n"
)


@dataclass
class CliJob:
    id: str
    role: str
    kind: str
    runner: str
    model: str
    effort: str
    prompt_path: Path
    worktree: Path
    writable: bool


@dataclass
class Outcome:
    id: str
    runner: str
    status: str
    output: dict | None
    usage: dict
    detail: str

    def to_dict(self) -> dict:
        return {"id": self.id, "runner": self.runner, "status": self.status, "output": self.output,
                "usage": self.usage, "detail": self.detail}


@dataclass
class _Run:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    missing: bool = False


def _executable(path: Path) -> str | None:
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def find_codex() -> str | None:
    """codex 실행 파일. 환경 변수 CASCADE_CODEX를 주면 그것만 본다."""
    if os.environ.get("CASCADE_CODEX"):
        return _executable(Path(os.environ["CASCADE_CODEX"]))
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    candidates = [Path(p) for p in _CODEX_APP_PATHS] + sorted(Path.home().glob(_CODEX_EXTENSION_GLOB))
    return next((found for found in map(_executable, candidates) if found), None)


def find_cursor_agent() -> str | None:
    """cursor-agent 실행 파일. 환경 변수 CASCADE_CURSOR_AGENT를 주면 그것만 본다."""
    if os.environ.get("CASCADE_CURSOR_AGENT"):
        return _executable(Path(os.environ["CASCADE_CURSOR_AGENT"]))
    on_path = shutil.which("cursor-agent")
    if on_path:
        return on_path
    return _executable(Path.home() / ".local" / "bin" / "cursor-agent")


def codex_available() -> bool:
    return find_codex() is not None and (Path.home() / ".codex" / "auth.json").is_file()


def cursor_available() -> bool:
    exe = find_cursor_agent()
    if exe is None:
        return False
    with tempfile.TemporaryDirectory(prefix="cascade-cache-") as tmp:
        home = cursor_home(Path(tmp), "review")
        run = _execute([exe, "status"], "", Path(tmp), {**os.environ, "HOME": str(home)}, STATUS_COMMAND_TIMEOUT)
    return not run.timed_out and not run.missing and "Logged in" in run.stdout + run.stderr


def codex_model(codex_dir: Path) -> str:
    try:
        config = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    model = config.get("model")
    return model if isinstance(model, str) else ""


def classify_failure(text: str) -> str:
    if _LIMIT_PATTERN.search(text):
        return STATUS_LIMIT
    if _AUTH_PATTERN.search(text):
        return STATUS_AUTH
    return STATUS_ERROR


def extract_json_block(text: str) -> dict | None:
    for block in reversed(_FENCED_JSON.findall(text)):
        try:
            value = json.loads(block)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    found = None
    pos = 0
    while (start := text.find("{", pos)) != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            pos = start + 1
            continue
        found, pos = value, end
    return found if isinstance(found, dict) else None


def cache_env(tmp: Path) -> dict[str, str]:
    return {
        "UV_CACHE_DIR": str(tmp / "uv"),
        "PIP_CACHE_DIR": str(tmp / "pip"),
        "XDG_CACHE_HOME": str(tmp / "xdg"),
        "npm_config_cache": str(tmp / "npm"),
    }


def cursor_home(base: Path, profile: str) -> Path:
    """임시 HOME. 키체인만 이어 로그인을 유지하고, 권한 설정으로 MCP·Web·홈 민감 경로 Read를 막는다."""
    if profile not in _PROFILE_DENY:
        raise ValueError(f"알 수 없는 Cursor 프로필: {profile}")
    real_home = Path.home()
    home = base / f"home-{profile}"
    (home / "Library").mkdir(parents=True, exist_ok=True)
    (home / ".cursor").mkdir(exist_ok=True)
    keychains = home / "Library" / "Keychains"
    if not keychains.is_symlink():
        keychains.symlink_to(real_home / "Library" / "Keychains")
    deny = list(_PROFILE_DENY[profile])
    deny += [f"Read({real_home}/{name}/**)" for name in _SENSITIVE_DIRS]
    deny.append(f"Read({real_home}/.netrc)")
    config = {"version": 1, "permissions": {"allow": [], "deny": deny}, "autoAcceptWebSearch": False}
    if profile == "review":
        config["approvalMode"] = "allowlist"
    (home / ".cursor" / "cli-config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return home


def _tool_roots_env() -> dict[str, str]:
    home = Path.home()
    return {key: str(home / rel) for key, rel in _TOOL_ROOTS.items()
            if key not in os.environ and (home / rel).is_dir()}


def _tail(text: str) -> str:
    return text.strip()[-DETAIL_TAIL:]


def _without_prompt(text: str, prompt: str) -> str:
    return text.replace(prompt.strip(), "") if prompt.strip() else text


def _execute(cmd: list[str], stdin: str, cwd: Path, env: dict, timeout: float) -> _Run:
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", cwd=cwd, env=env, start_new_session=True)
    except OSError as e:
        return _Run(-1, "", str(e), missing=True)
    try:
        stdout, stderr = proc.communicate(stdin, timeout=timeout)
        return _Run(proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            stdout = stderr = ""
        return _Run(proc.returncode, stdout, stderr, timed_out=True)


def _codex_failure_text(stdout: str, stderr: str) -> str:
    """에이전트 대화는 빼고 오류 이벤트의 메시지, JSON이 아닌 줄, stderr만 모은다."""
    parts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            parts.append(line)
            continue
        if isinstance(event, dict) and event.get("type") in ("error", "turn.failed"):
            error = event.get("error")
            parts.append(str(event.get("message") or (error.get("message") if isinstance(error, dict) else error) or ""))
    parts.append(stderr)
    return "\n".join(parts)


def _unexpected(job: CliJob, e: Exception) -> Outcome:
    return Outcome(job.id, job.runner, STATUS_ERROR, None, {}, f"예상 밖 오류: {type(e).__name__}: {e}")


def _failure(job: CliJob, run: _Run, usage: dict, timeout: float) -> Outcome | None:
    if run.missing:
        return Outcome(job.id, job.runner, STATUS_ERROR, None, usage, f"실행 파일을 실행하지 못했다: {_tail(run.stderr)}")
    if run.timed_out:
        return Outcome(job.id, job.runner, STATUS_TIMEOUT, None, usage, f"시간 초과({timeout}초)")
    return None


def _check_output(job: CliJob, output: dict | None) -> dict | None:
    if output is None:
        return None
    candidate = {**output, "model_id": f"{job.runner}:{job.model}"}
    return candidate if valid_output(candidate, job.role) else None


def _add_usage(total: dict, extra: dict) -> dict:
    return {key: total.get(key, 0) + extra.get(key, 0) for key in {*total, *extra}}


_RENAME_SWAP = 0x2
_AT_FDCWD = -100


def _swap(a: Path, b: Path) -> bool:
    """두 파일을 원자적으로 맞바꾼다. 운영체제가 지원하지 않으면 False."""
    libc = ctypes.CDLL(None, use_errno=True)
    a_raw, b_raw = os.fsencode(a), os.fsencode(b)
    if hasattr(libc, "renamex_np"):
        result = libc.renamex_np(a_raw, b_raw, ctypes.c_uint(_RENAME_SWAP))
    elif hasattr(libc, "renameat2"):
        result = libc.renameat2(_AT_FDCWD, a_raw, _AT_FDCWD, b_raw, ctypes.c_uint(_RENAME_SWAP))
    else:
        return False
    if result != 0:
        err = ctypes.get_errno()
        if err in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS):
            return False
        raise OSError(err, os.strerror(err), str(a), None, str(b))
    return True


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _replace_if_unchanged(auth: Path, refreshed: bytes, snapshot: bytes) -> bool:
    """맞바꾼 순간 원본 자리에 있던 내용이 snapshot과 다르면(잠금 밖 프로세스가 끼어들면) 되돌린다."""
    tmp = auth.with_name(f".{auth.name}.cascade-{os.getpid()}-{id(refreshed)}")
    try:
        _write_private(tmp, refreshed)
        if not _swap(tmp, auth):
            os.replace(tmp, auth)
            return True
        if tmp.read_bytes() == snapshot:
            return True
        _swap(tmp, auth)
        return False
    finally:
        tmp.unlink(missing_ok=True)


class _AuthLock:
    def __init__(self, auth: Path):
        self.path = auth.with_name(f"{auth.name}.lock")

    def __enter__(self):
        self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        os.close(self.fd)


class CodexRunner:
    def __init__(self, exe: str, codex_dir: Path | None = None, timeout: int = JOB_TIMEOUT):
        self.exe = exe
        self.codex_dir = Path(codex_dir) if codex_dir else Path.home() / ".codex"
        self.timeout = timeout

    def warmup_needed(self, now: datetime) -> bool:
        """auth.json의 last_refresh가 없거나 오래됐으면 토큰이 곧 갱신될 수 있다."""
        try:
            refreshed = datetime.fromisoformat(json.loads((self.codex_dir / "auth.json").read_text(encoding="utf-8"))["last_refresh"])
        except (OSError, ValueError, KeyError, TypeError):
            return True
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now - refreshed >= CODEX_WARMUP_AGE

    def _prepare_home(self, home: Path, job: CliJob) -> bytes:
        auth = self.codex_dir / "auth.json"
        home.mkdir()
        with _AuthLock(auth):
            snapshot = auth.read_bytes()
        _write_private(home / "auth.json", snapshot)
        lines = []
        if job.model:
            lines.append(f"model = {json.dumps(job.model, ensure_ascii=False)}")
        lines += [
            f"model_reasoning_effort = {json.dumps(job.effort)}",
            "include_permissions_instructions = false",
            "",
            "[skills]",
            "include_instructions = false",
            "",
            f"[projects.{json.dumps(str(job.worktree), ensure_ascii=False)}]",
            'trust_level = "trusted"',
        ]
        (home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return snapshot

    def _write_back(self, copied: Path, snapshot: bytes) -> str:
        """복사본에서 갱신된 토큰을 원본에 되돌려 쓴다. 원본이 복사 뒤 바뀌었으면 덮지 않고 경고를 돌려 준다."""
        try:
            refreshed = copied.read_bytes()
        except OSError:
            return ""
        if refreshed == snapshot:
            return ""
        try:
            valid = isinstance(json.loads(refreshed), dict)
        except ValueError:
            valid = False
        if not valid:
            return "auth.json 복사본이 올바른 JSON이 아니어서(쓰는 중 끝났을 수 있다) 되돌려 쓰지 않았다"
        auth = self.codex_dir / "auth.json"
        with _AuthLock(auth):
            try:
                current = auth.read_bytes()
            except OSError:
                current = None
            if current == snapshot and _replace_if_unchanged(auth, refreshed, snapshot):
                return ""
        return "auth.json 충돌: 실행 중 원본이 바뀌어 갱신된 토큰을 되돌려 쓰지 않았다. codex login이 필요할 수 있다"

    def run(self, job: CliJob) -> Outcome:
        try:
            return self._run_job(job)
        except Exception as e:
            return _unexpected(job, e)

    def _run_job(self, job: CliJob) -> Outcome:
        if not (self.codex_dir / "auth.json").is_file():
            return Outcome(job.id, job.runner, STATUS_AUTH, None, {}, f"{self.codex_dir / 'auth.json'} 없음: codex login 필요")
        prompt = job.prompt_path.read_text(encoding="utf-8")
        tmp = Path(tempfile.mkdtemp(prefix="cascade-cache-"))
        try:
            return self._run(job, prompt, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _run(self, job: CliJob, prompt: str, tmp: Path) -> Outcome:
        home, schema_file, result_file = tmp / "codex-home", tmp / "schema.json", tmp / "result.json"
        snapshot = self._prepare_home(home, job)
        schema_file.write_text(json.dumps(SCHEMAS[job.kind], ensure_ascii=False), encoding="utf-8")
        cmd = [
            self.exe, "exec",
            "-s", "workspace-write" if job.writable else "read-only",
            "--ephemeral", "--json", "--skip-git-repo-check",
            "-C", str(job.worktree),
            *(["-m", job.model] if job.model else []),
            "-c", f"model_reasoning_effort={json.dumps(job.effort)}",
            "--output-schema", str(schema_file),
            "-o", str(result_file),
            "-",
        ]
        env = {**os.environ, **cache_env(tmp), "CODEX_HOME": str(home)}
        try:
            run = _execute(cmd, prompt, job.worktree, env, self.timeout)
        finally:
            conflict = self._write_back(home / "auth.json", snapshot)
        usage = _codex_usage(run.stdout)
        outcome = self._outcome(job, run, prompt, usage, result_file)
        if conflict:
            outcome.detail = f"{outcome.detail}; {conflict}" if outcome.detail else conflict
        return outcome

    def _outcome(self, job: CliJob, run: _Run, prompt: str, usage: dict, result_file: Path) -> Outcome:
        failed = _failure(job, run, usage, self.timeout)
        if failed:
            return failed
        if run.returncode != 0:
            text = _without_prompt(_codex_failure_text(run.stdout, run.stderr), prompt)
            return Outcome(job.id, job.runner, classify_failure(text), None, usage,
                           f"종료 코드 {run.returncode}: {_tail(text) or _tail(run.stdout)}")
        if not result_file.is_file():
            return Outcome(job.id, job.runner, STATUS_ERROR, None, usage, "결과 파일이 없다")
        try:
            raw = json.loads(result_file.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            return Outcome(job.id, job.runner, STATUS_INVALID, None, usage, "결과가 JSON이 아니다")
        output = _check_output(job, raw if isinstance(raw, dict) else None)
        if output is None:
            return Outcome(job.id, job.runner, STATUS_INVALID, None, usage, "결과가 스키마를 따르지 않는다")
        return Outcome(job.id, job.runner, STATUS_OK, output, usage, "")


def _codex_usage(stdout: str) -> dict:
    usage = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {k: v for k, v in event["usage"].items() if isinstance(v, int)}
    return usage


_CURSOR_USAGE_KEYS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadTokens": "cached_input_tokens",
    "cacheWriteTokens": "cache_write_tokens",
}


def _cursor_envelope(stdout: str) -> dict | None:
    envelope = None
    for text in [stdout, *stdout.splitlines()]:
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            envelope = data
            if text is stdout:
                break
    return envelope


def _cursor_usage(envelope: dict | None) -> dict:
    raw = envelope.get("usage") if envelope else None
    if not isinstance(raw, dict):
        return {}
    return {name: raw[key] for key, name in _CURSOR_USAGE_KEYS.items() if isinstance(raw.get(key), int)}


class CursorRunner:
    def __init__(self, exe: str, timeout: int = JOB_TIMEOUT):
        self.exe = exe
        self.timeout = timeout

    def run(self, job: CliJob) -> Outcome:
        try:
            return self._run_job(job)
        except Exception as e:
            return _unexpected(job, e)

    def _run_job(self, job: CliJob) -> Outcome:
        schema = json.dumps(SCHEMAS[job.kind], ensure_ascii=False)
        prompt = job.prompt_path.read_text(encoding="utf-8") + _OUTPUT_INSTRUCTION.format(schema=schema)
        tmp = Path(tempfile.mkdtemp(prefix="cascade-cache-"))
        try:
            home = cursor_home(tmp, "worker" if job.writable else "readonly")
            env = {**os.environ, **_tool_roots_env(), **cache_env(tmp), "HOME": str(home)}
            return self._run(job, prompt, schema, env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _command(self, job: CliJob, resume: str | None) -> list[str]:
        cmd = [self.exe, "-p", "--trust", "--sandbox", "enabled", "--output-format", "json",
               "--model", job.model, "--workspace", str(job.worktree)]
        return cmd + ["--resume", resume] if resume else cmd

    def _call(self, job: CliJob, stdin: str, env: dict, resume: str | None = None) -> tuple[Outcome | None, dict | None, dict]:
        run = _execute(self._command(job, resume), stdin, job.worktree, env, self.timeout)
        envelope = _cursor_envelope(run.stdout)
        usage = _cursor_usage(envelope)
        failed = _failure(job, run, usage, self.timeout)
        if failed:
            return failed, None, usage
        text = _without_prompt(f"{run.stdout}\n{run.stderr}", stdin)
        if envelope is None:
            status = classify_failure(text)
            return Outcome(job.id, job.runner, status, None, usage,
                           f"JSON 봉투가 아니다(종료 코드 {run.returncode}): {_tail(_without_prompt(run.stderr, stdin) or run.stdout)}"), None, usage
        if run.returncode != 0 or envelope.get("is_error"):
            message = str(envelope.get("result", "")) if envelope.get("is_error") else ""
            text = _without_prompt(f"{message}\n{run.stderr}", stdin)
            return Outcome(job.id, job.runner, classify_failure(text), None, usage,
                           f"종료 코드 {run.returncode}: {_tail(message) or _tail(run.stderr) or _tail(str(envelope.get('result', '')))}"), None, usage
        return None, envelope, usage

    def _run(self, job: CliJob, prompt: str, schema: str, env: dict) -> Outcome:
        failed, envelope, usage = self._call(job, prompt, env)
        if failed:
            return failed
        output = _check_output(job, extract_json_block(str(envelope.get("result", ""))))
        if output is None:
            session = envelope.get("session_id")
            if not session:
                return Outcome(job.id, job.runner, STATUS_INVALID, None, usage, "결과가 스키마를 따르지 않는다(session_id 없음)")
            failed, envelope, retry_usage = self._call(job, _RETRY_PROMPT.format(schema=schema), env, resume=session)
            usage = _add_usage(usage, retry_usage)
            if failed:
                failed.usage = usage
                return failed
            output = _check_output(job, extract_json_block(str(envelope.get("result", ""))))
            if output is None:
                return Outcome(job.id, job.runner, STATUS_INVALID, None, usage, "재요청 뒤에도 스키마를 따르지 않는 결과")
        return Outcome(job.id, job.runner, STATUS_OK, output, usage, "")


READONLY_CHANGED = "읽기 역할이 파일을 바꿔 되돌림"
_STOPPING = (STATUS_LIMIT, STATUS_AUTH)


_tree_locks: dict[Path, threading.Lock] = {}
_tree_locks_guard = threading.Lock()


def _tree_lock(wt: Path) -> threading.Lock:
    """같은 워크트리에서 동시에 도는 작업들이 인덱스 잠금을 두고 다투지 않게 git 호출을 한 줄로 세운다."""
    with _tree_locks_guard:
        return _tree_locks.setdefault(Path(wt).resolve(), threading.Lock())


def tree_of(wt: Path) -> str:
    """워크트리 전체(추적하지 않는 파일 포함)의 현재 내용을 가리키는 트리 해시."""
    with _tree_lock(wt):
        _git_checked(wt, *NO_HOOKS, "add", "-A")
        return _git_checked(wt, *NO_HOOKS, "write-tree").strip()


def restore_tree(wt: Path, tree: str) -> None:
    """워크트리를 tree 내용으로 되돌린다. 먼저 새 파일을 인덱스에 올려 read-tree가 지울 수 있게 한다."""
    with _tree_lock(wt):
        _git_checked(wt, *NO_HOOKS, "add", "-A")
        _git_checked(wt, *NO_HOOKS, "read-tree", "-u", "--reset", tree)


class _Exhaustion:
    """한도·인증으로 멈춘 runner와 그 원인. 아직 시작하지 않은 작업이 건너뛸 때 읽는다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._reason: dict[str, str] = {}

    def reason(self, runner: str) -> str | None:
        with self._lock:
            return self._reason.get(runner)

    def record(self, outcome: Outcome) -> None:
        if outcome.status in _STOPPING:
            with self._lock:
                self._reason.setdefault(outcome.runner, f"{outcome.id}에서 {outcome.status}: {outcome.detail}")


def _skipped(job: CliJob, reason: str) -> Outcome:
    return Outcome(job.id, job.runner, STATUS_SKIPPED, None, {}, f"{job.runner} 실행기가 멈춰 건너뜀({reason})")


def _failed(job: CliJob, detail: str) -> Outcome:
    return Outcome(job.id, job.runner, STATUS_ERROR, None, {}, detail)


def _restored(outcome: Outcome, wt: Path, tree: str, note: str) -> Outcome:
    try:
        restore_tree(wt, tree)
    except Exception as e:
        return Outcome(outcome.id, outcome.runner, STATUS_ERROR, None, outcome.usage,
                       f"워크트리를 되돌리지 못했다: {e}")
    if note:
        outcome.detail = f"{outcome.detail}; {note}" if outcome.detail else note
    return outcome


def _run_isolated(job: CliJob, runner: object) -> Outcome:
    try:
        before = tree_of(job.worktree)
    except Exception as e:
        return _failed(job, f"실행 전 워크트리 상태를 기록하지 못했다: {e}")
    try:
        outcome = runner.run(job)
    except Exception as e:
        outcome = _unexpected(job, e)
    if job.writable:
        return outcome if outcome.status == STATUS_OK else _restored(outcome, job.worktree, before, "")
    try:
        changed = tree_of(job.worktree) != before
    except Exception as e:
        return _failed(job, f"실행 뒤 워크트리 상태를 확인하지 못했다: {e}")
    return _restored(outcome, job.worktree, before, READONLY_CHANGED) if changed else outcome


def _run_one(job: CliJob, runners: dict[str, object], exhausted: _Exhaustion) -> Outcome:
    reason = exhausted.reason(job.runner)
    if reason:
        return _skipped(job, reason)
    runner = runners.get(job.runner)
    if runner is None:
        return _failed(job, f"{job.runner} 실행기가 없다")
    outcome = _run_isolated(job, runner)
    exhausted.record(outcome)
    return outcome


def _run_group(group: list[tuple[int, CliJob]], runner: object, runners: dict[str, object], exhausted: _Exhaustion,
               max_parallel: int, now: datetime, results: list[Outcome | None]) -> None:
    def run(item: tuple[int, CliJob]) -> None:
        index, job = item
        results[index] = _run_one(job, runners, exhausted)

    warmup = getattr(runner, "warmup_needed", None)
    if warmup is not None and warmup(now):
        run(group[0])
        group = group[1:]
    with ThreadPoolExecutor(max_parallel) as pool:
        list(pool.map(run, group))


def run_all(jobs: list[CliJob], runners: dict[str, object], max_parallel: int = MAX_PARALLEL,
            now: datetime | None = None) -> list[Outcome]:
    """runner별로 작업을 동시에 돌린다. 반환 순서는 입력과 같다."""
    now = now or datetime.now(timezone.utc)
    results: list[Outcome | None] = [None] * len(jobs)
    groups: dict[str, list[tuple[int, CliJob]]] = {}
    for index, job in enumerate(jobs):
        groups.setdefault(job.runner, []).append((index, job))
    exhausted = _Exhaustion()
    with ThreadPoolExecutor(max(len(groups), 1)) as outer:
        futures = [outer.submit(_run_group, group, runners.get(name), runners, exhausted, max_parallel, now, results)
                   for name, group in groups.items()]
        for future in futures:
            future.result()
    return results
