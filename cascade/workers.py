from pathlib import Path

from cascade.jobs import ROLES, RUNNER_CLAUDE
from cascade.runners import RUNNER_CODEX, RUNNER_CURSOR, codex_available, codex_model, cursor_available

WORKERS_CLAUDE, WORKERS_EXTERNAL = "claude", "external"
WORKERS_CHOICES = (WORKERS_CLAUDE, WORKERS_EXTERNAL)
DEFAULT_CURSOR_MODEL = "composer-2.5"
WORKER_ROLES = ("a", "b", "writer", "checker")
DEFAULT_MODEL_NAME = "default"

_SEATS = {"A·writer": ("a", "writer"), "B·checker": ("b", "checker")}
_LABELS = {RUNNER_CLAUDE: "Claude", RUNNER_CODEX: "Codex", RUNNER_CURSOR: "Cursor"}
_RECIPIENTS = {RUNNER_CODEX: "OpenAI", RUNNER_CURSOR: "Cursor(팀 프라이버시 모드)"}


def _claude(role: str) -> dict:
    model, effort = ROLES[role]
    return {"runner": RUNNER_CLAUDE, "model": model, "effort": effort}


def _external(role: str, runner: str, model: str) -> dict:
    return {"runner": runner, "model": model, "effort": ROLES[role][1]}


def assign(choice: str, available: dict[str, bool], codex_model: str, cursor_model: str) -> dict[str, dict]:
    runners = {role: RUNNER_CLAUDE for role in WORKER_ROLES}
    if choice == WORKERS_EXTERNAL:
        usable = [r for r in (RUNNER_CODEX, RUNNER_CURSOR) if available.get(r)]
        for seat, runner in zip(_SEATS.values(), usable):
            runners.update({role: runner for role in seat})
    models = {RUNNER_CODEX: codex_model, RUNNER_CURSOR: cursor_model}
    return {role: _claude(role) if runner == RUNNER_CLAUDE else _external(role, runner, models[runner])
            for role, runner in runners.items()}


def assign_available(choice: str, cursor_model: str) -> dict[str, dict]:
    if choice != WORKERS_EXTERNAL:
        return {}
    available = {RUNNER_CODEX: codex_available(), RUNNER_CURSOR: cursor_available()}
    return assign(choice, available, codex_model(Path.home() / ".codex"), cursor_model)


def demote(workers: dict[str, dict], runner: str) -> dict[str, dict]:
    return {role: _claude(role) if spec["runner"] == runner else dict(spec) for role, spec in workers.items()}


def expected_model_id(spec: dict) -> str:
    if spec["runner"] == RUNNER_CLAUDE:
        return spec["model"]
    return f"{spec['runner']}:{spec['model'] or DEFAULT_MODEL_NAME}"


def _label(spec: dict) -> str:
    return f"{_LABELS.get(spec['runner'], spec['runner'])}({spec['model'] or DEFAULT_MODEL_NAME})"


def describe(workers: dict[str, dict]) -> str:
    return ", ".join(f"{name}={_label(workers[roles[0]])}" for name, roles in _SEATS.items())


def has_external(workers: dict[str, dict] | None) -> bool:
    return any(spec["runner"] != RUNNER_CLAUDE for spec in (workers or {}).values())


def consent_line(workers: dict[str, dict] | None) -> str:
    used = {spec["runner"] for spec in (workers or {}).values()}
    targets = [_RECIPIENTS[r] for r in (RUNNER_CODEX, RUNNER_CURSOR) if r in used]
    return f"작업자 전송 동의: 워크트리의 코드·문서를 {', '.join(targets)}로 보낸다" if targets else ""
