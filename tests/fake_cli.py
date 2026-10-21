import json
import stat
import sys
from pathlib import Path

_COMMON = r'''
import json, os, sys, time
from pathlib import Path

log = Path(os.environ["FAKE_CLI_LOG"])
log.mkdir(parents=True, exist_ok=True)


def claim(name):
    n = 1
    while True:
        try:
            fd = os.open(log / f"{name}-{n}.args.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            n += 1
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(sys.argv[1:], f)
        return n


def stamp(name, n, event):
    with open(log / f"{name}-{n}.{event}", "w") as f:
        f.write(repr(time.time()))


def commit(root):
    import subprocess
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=fake@example.com", "-c", "user.name=fake",
                    "commit", "-q", "--no-verify", "--allow-empty", "-m", "fake cli commit"], check=True, capture_output=True)


def mutate(root):
    spec = os.environ.get("FAKE_MUTATE", "")
    for item in filter(None, spec.split(";")):
        rel, _, content = item.partition("=")
        target = Path(root) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for key, files in json.loads(os.environ.get("FAKE_MUTATE_WT", "{}")).items():
        if key in Path(root).name:
            for rel, content in files.items():
                target = Path(root) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    if os.environ.get("FAKE_COMMIT"):
        commit(root)
'''

CODEX_BODY = _COMMON + r'''
if sys.argv[1:2] == ["sandbox"]:
    import subprocess
    n = claim("codex-sandbox")
    args = sys.argv[2:]
    (log / f"codex-sandbox-{n}.env.json").write_text(json.dumps({"CODEX_HOME": os.environ.get("CODEX_HOME")}))
    command = args[args.index("--") + 1:]
    if command[-3:] == ["sh", "-c", ":"]:
        probe_exit = os.environ.get("FAKE_CODEX_SANDBOX_PROBE_EXIT")
        if probe_exit:
            sys.stderr.write(os.environ.get("FAKE_CODEX_SANDBOX_PROBE_STDERR", ""))
            sys.exit(int(probe_exit))
    elif os.environ.get("FAKE_CODEX_SANDBOX_EXIT"):
        sys.exit(int(os.environ["FAKE_CODEX_SANDBOX_EXIT"]))
    sys.exit(subprocess.run(command, cwd=args[args.index("-C") + 1]).returncode)
n = claim("codex")
stamp("codex", n, "start")
args = sys.argv[1:]
home = Path(os.environ["CODEX_HOME"])
(log / f"codex-{n}.env.json").write_text(json.dumps({
    k: os.environ.get(k) for k in ("CODEX_HOME", "UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache")}))
(log / f"codex-{n}.config.toml").write_text((home / "config.toml").read_text())
(log / f"codex-{n}.auth-is-link").write_text(str((home / "auth.json").is_symlink()))
if args and args[-1] == "-":
    (log / f"codex-{n}.stdin").write_text(sys.stdin.read())
sleep = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
mutate(args[args.index("-C") + 1])
if os.environ.get("FAKE_CODEX_REFRESH"):
    (home / "auth.json").write_text(os.environ["FAKE_CODEX_REFRESH"])
if os.environ.get("FAKE_CODEX_TOUCH_ORIGINAL"):
    Path(os.environ["FAKE_CODEX_ORIGINAL_AUTH"]).write_text(os.environ["FAKE_CODEX_TOUCH_ORIGINAL"])
if "-o" in args and "FAKE_CODEX_RESULT" in os.environ:
    Path(args[args.index("-o") + 1]).write_text(os.environ["FAKE_CODEX_RESULT"])
print(json.dumps({"type": "thread.started"}))
for event in json.loads(os.environ.get("FAKE_CODEX_EVENTS", "[]")):
    print(json.dumps(event))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 7, "reasoning_output_tokens": 2}}))
sys.stderr.write(os.environ.get("FAKE_CODEX_STDERR", ""))
stamp("codex", n, "end")
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
'''

CURSOR_BODY = _COMMON + r'''
if sys.argv[1:] == ["status"]:
    claim("cursor-status")
    print(os.environ.get("FAKE_CURSOR_STATUS", "✓ Logged in as tester@example.com"))
    sys.exit(0)
n = claim("cursor-agent")
stamp("cursor-agent", n, "start")
home = Path(os.environ["HOME"])
(log / f"cursor-agent-{n}.stdin").write_text(sys.stdin.read())
(log / f"cursor-agent-{n}.env.json").write_text(json.dumps({
    k: os.environ.get(k) for k in (
        "HOME", "PYENV_ROOT", "UV_PYTHON_INSTALL_DIR", "UV_CACHE_DIR", "PIP_CACHE_DIR", "XDG_CACHE_HOME", "npm_config_cache")}))
config = home / ".cursor" / "cli-config.json"
if config.is_file():
    (log / f"cursor-agent-{n}.config.json").write_text(config.read_text())
sleep = float(os.environ.get("FAKE_CURSOR_SLEEP", "0"))
if sleep:
    time.sleep(sleep)
workspace = sys.argv[sys.argv.index("--workspace") + 1]
mutate(workspace)
responses = json.loads(os.environ.get("FAKE_CURSOR_RESULTS", '[""]'))
matched = [e for e in responses if isinstance(e, dict) and e.get("match") and e["match"] in workspace]
plain = [e for e in responses if not (isinstance(e, dict) and "match" in e)] or [""]
entry = matched[0] if matched else plain[min(n, len(plain)) - 1]
if isinstance(entry, str):
    entry = {"result": entry}
if "stdout" in entry:
    sys.stdout.write(entry["stdout"])
else:
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": entry.get("is_error", False),
        "result": entry.get("result", ""), "session_id": "s",
        "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 5, "cacheWriteTokens": 1}}))
sys.stderr.write(entry.get("stderr", ""))
stamp("cursor-agent", n, "end")
sys.exit(entry.get("exit", 0))
'''


def install(directory, name, body):
    path = Path(directory) / name
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def result_json(**overrides):
    result = {"verdict": "사실", "needs_design_decision": False, "reason": "재현함", "verify": [], "model_id": "self-reported"}
    result.update(overrides)
    return json.dumps(result, ensure_ascii=False)


def fenced(text):
    return f"설명\n```json\n{text}\n```\n"
