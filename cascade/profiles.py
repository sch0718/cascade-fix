import copy
import tomllib
from pathlib import Path

from cascade.config import ConfigError
from cascade.jobs import ROLES, RUNNER_CLAUDE
from cascade.review import EFFORTS
from cascade.runners import RUNNER_CODEX, RUNNER_CURSOR
from cascade.workers import DEFAULT_CURSOR_MODEL

RUNNER_AUTO = "auto"
BUILTIN_NAMES = ("default", "claude-only", "local")
ROLE_KEYS = (
    "code.review", "code.a", "code.b", "code.adjudicator", "code.opinion", "code.recheck",
    "doc.review", "doc.judge_a", "doc.judge_b", "doc.writer", "doc.checker", "doc.adjudicator",
    "doc.opinion", "doc.recheck",
)
CELL_KEYS = ("runner", "model", "effort", "fallback")
FALLBACK_KEYS = ("runner", "model", "effort")
PROFILE_KEYS = ("base", "adopt_on_both_pass")

_REVIEW_ROLES = ("review", "opinion", "recheck")
_ROLES_SOURCE = {
    "review": "reviewer", "opinion": "reviewer", "recheck": "reviewer",
    "a": "a", "b": "b", "judge_a": "a", "judge_b": "b",
    "writer": "writer", "checker": "checker", "adjudicator": "adjudicator",
}
_FOREIGN_MODEL_PREFIXES = ("claude-", "gpt-")


class ProfileError(ConfigError):
    pass


def _role(key: str) -> str:
    if key not in ROLE_KEYS:
        raise ProfileError(f"모르는 칸 키다: {key!r}. 알려진 칸 키: {', '.join(ROLE_KEYS)}")
    return key.split(".", 1)[1]


def allowed_runners(key: str) -> tuple[str, ...]:
    role = _role(key)
    if role == "adjudicator":
        return (RUNNER_CLAUDE,)
    if role in _REVIEW_ROLES:
        return (RUNNER_CLAUDE, RUNNER_CODEX, RUNNER_CURSOR, RUNNER_AUTO)
    return (RUNNER_CLAUDE, RUNNER_CODEX, RUNNER_CURSOR)


def _claude_cell(key: str) -> dict:
    model, effort = ROLES[_ROLES_SOURCE[_role(key)]]
    return {"runner": RUNNER_CLAUDE, "model": model, "effort": effort}


def _claude_only_cell(key: str) -> dict:
    cell = _claude_cell(key)
    return {**cell, "fallback": dict(cell)}


def _external_cell(key: str, runner: str, effort: str) -> dict:
    return {"runner": runner, "model": "", "effort": effort, "fallback": dict(_claude_only_cell(key)["fallback"])}


def _claude_only() -> dict:
    return {"adopt_on_both_pass": True, "roles": {key: _claude_only_cell(key) for key in ROLE_KEYS}}


def _default() -> dict:
    roles = _claude_only()["roles"]
    for key in ROLE_KEYS:
        if _role(key) in _REVIEW_ROLES:
            roles[key]["runner"] = RUNNER_AUTO
    return {"adopt_on_both_pass": False, "roles": roles}


def _local() -> dict:
    profile = _claude_only()
    roles = profile["roles"]
    for key in ROLE_KEYS:
        if _role(key) in _REVIEW_ROLES:
            roles[key] = _external_cell(key, RUNNER_CODEX, "")
    roles["code.a"] = _external_cell("code.a", RUNNER_CODEX, ROLES["a"][1])
    return profile


_BUILDERS = {"default": _default, "claude-only": _claude_only, "local": _local}


def builtin(name: str) -> dict:
    if name not in _BUILDERS:
        raise ProfileError(f"모르는 내장 프로필이다: {name!r}. 알려진 이름: {', '.join(BUILTIN_NAMES)}")
    return _BUILDERS[name]()


def reset_cell(key: str, runner: str, cursor_model: str = DEFAULT_CURSOR_MODEL) -> dict:
    claude_only = _claude_only_cell(key)
    if runner in (RUNNER_CLAUDE, RUNNER_AUTO):
        return {**claude_only, "runner": runner}
    if runner == RUNNER_CURSOR:
        return {**claude_only, "runner": runner, "model": cursor_model, "effort": ""}
    effort = "" if _role(key) in _REVIEW_ROLES else claude_only["effort"]
    return {**claude_only, "runner": runner, "model": "", "effort": effort}


def _merge_values(key: str, base: dict, override: dict, cursor_model: str) -> dict:
    new_runner = override.get("runner")
    if new_runner is not None and new_runner != base["runner"]:
        merged = reset_cell(key, new_runner, cursor_model)
        merged.pop("fallback", None)
    else:
        merged = {k: base[k] for k in FALLBACK_KEYS}
    for field in ("model", "effort"):
        if field in override:
            merged[field] = override[field]
    return merged


def merge_cell(key: str, base: dict, override: dict, cursor_model: str = DEFAULT_CURSOR_MODEL) -> dict:
    merged = _merge_values(key, base, override, cursor_model)
    merged["fallback"] = _merge_values(key, base["fallback"], override.get("fallback", {}), cursor_model)
    return merged


def _fail(name: str, key: str | None, message: str) -> ProfileError:
    where = f"profiles.{name}" if key is None else f'profiles.{name} 칸 "{key}"'
    return ProfileError(f"{where}: {message}")


def _check_values(name: str, key: str, cell: dict, *, fallback: bool) -> None:
    where = f"{key}의 fallback" if fallback else key
    runner, model, effort = cell["runner"], cell["model"], cell["effort"]
    allowed = tuple(r for r in allowed_runners(key) if not (fallback and r == RUNNER_AUTO))
    if runner not in allowed:
        raise _fail(name, where, f"runner {runner!r}는 받지 않는다. 받는 runner: {', '.join(allowed)}")
    if effort and effort not in EFFORTS:
        raise _fail(name, where, f"effort {effort!r}는 받지 않는다. 받는 값: {', '.join(EFFORTS)}")
    if runner == RUNNER_CLAUDE and not model.startswith("claude-"):
        raise _fail(name, where, f"claude runner의 model은 claude-로 시작해야 한다: {model!r}")
    if runner == RUNNER_CURSOR:
        if effort:
            raise _fail(name, where, f"cursor runner는 effort를 쓰지 않는다: {effort!r}")
        if model.startswith(_FOREIGN_MODEL_PREFIXES):
            raise _fail(name, where, f"cursor runner에는 Claude·GPT 계열 모델을 쓸 수 없다: {model!r}")
    if runner == RUNNER_CODEX and _role(key) in _REVIEW_ROLES and (model or effort):
        raise _fail(name, where, "codex 검토 역할은 model·effort를 비워야 한다")


def _check_shape(name: str, key: str, cell, keys: tuple[str, ...], *, partial: bool = False) -> None:
    if not isinstance(cell, dict):
        raise _fail(name, key, f"칸은 표여야 한다: {cell!r}")
    unknown = sorted(set(cell) - set(keys))
    if unknown:
        raise _fail(name, key, f"모르는 값 키다: {', '.join(unknown)}. 알려진 값 키: {', '.join(keys)}")
    for field in keys:
        if field == "fallback":
            continue
        if partial and field not in cell:
            continue
        if not isinstance(cell.get(field), str):
            raise _fail(name, key, f"{field}는 문자열이어야 한다: {cell.get(field)!r}")


def validate(name: str, profile: dict) -> None:
    if not isinstance(profile.get("adopt_on_both_pass"), bool):
        raise _fail(name, None, f"adopt_on_both_pass는 참/거짓이어야 한다: {profile.get('adopt_on_both_pass')!r}")
    roles = profile.get("roles")
    if not isinstance(roles, dict):
        raise _fail(name, None, "roles는 표여야 한다")
    unknown = sorted(set(roles) - set(ROLE_KEYS))
    if unknown:
        raise _fail(name, None, f"모르는 칸 키다: {', '.join(unknown)}. 알려진 칸 키: {', '.join(ROLE_KEYS)}")
    missing = [key for key in ROLE_KEYS if key not in roles]
    if missing:
        raise _fail(name, None, f"빠진 칸이 있다: {', '.join(missing)}")
    for key in ROLE_KEYS:
        cell = roles[key]
        _check_shape(name, key, cell, CELL_KEYS)
        _check_values(name, key, cell, fallback=False)
        fallback = cell.get("fallback")
        _check_shape(name, f"{key}의 fallback", fallback, FALLBACK_KEYS)
        _check_values(name, key, fallback, fallback=True)


def _parse_entry(name: str, entry) -> tuple[str | None, bool | None, dict]:
    if not isinstance(entry, dict):
        raise _fail(name, None, f"프로필은 표여야 한다: {entry!r}")
    base = entry.get("base")
    if base is not None and not isinstance(base, str):
        raise _fail(name, None, f"base는 문자열이어야 한다: {base!r}")
    adopt = entry.get("adopt_on_both_pass")
    if adopt is not None and not isinstance(adopt, bool):
        raise _fail(name, None, f"adopt_on_both_pass는 참/거짓이어야 한다: {adopt!r}")
    overrides = {}
    for key, cell in entry.items():
        if key in PROFILE_KEYS:
            continue
        if key not in ROLE_KEYS:
            hint = " 점이 든 칸 키는 따옴표로 감싼다(\"code.a\" = { ... })." if key in ("code", "doc") else ""
            raise _fail(name, None, f"모르는 칸 키다: {key!r}. 알려진 칸 키: {', '.join(ROLE_KEYS)}.{hint}")
        _check_shape(name, key, cell, CELL_KEYS, partial=True)
        if "fallback" in cell:
            _check_shape(name, f"{key}의 fallback", cell["fallback"], FALLBACK_KEYS, partial=True)
        overrides[key] = cell
    return base, adopt, overrides


def load_profiles(config_path: Path | None) -> tuple[dict[str, dict], dict]:
    found = {n: builtin(n) for n in BUILTIN_NAMES}
    if config_path is None or not Path(config_path).is_file():
        return found, {}
    try:
        with Path(config_path).open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"repos.toml을 읽지 못했다: {e}") from e
    raw = data.get("profiles", {})
    if not isinstance(raw, dict):
        raise ProfileError(f"profiles는 표여야 한다: {raw!r}")
    if "claude-only" in raw:
        raise _fail("claude-only", None, "claude-only는 덮을 수 없다. 바꾸려면 base = \"claude-only\"로 새 이름을 만든다")
    parsed = {name: _parse_entry(name, entry) for name, entry in raw.items()}
    resolved: dict[str, dict] = {}

    def resolve(name: str, stack: tuple[str, ...]) -> dict:
        if name in resolved:
            return resolved[name]
        base, adopt, overrides = parsed[name]
        if name in BUILTIN_NAMES:
            if base is not None:
                raise _fail(name, None, "내장 프로필을 덮을 때는 base를 쓸 수 없다. base를 쓰려면 새 이름을 만든다")
            profile = builtin(name)
        else:
            base = base or "default"
            if base in stack or base == name:
                raise _fail(name, None, f"base가 순환한다: {' → '.join((*stack, name, base))}")
            if base in parsed:
                profile = copy.deepcopy(resolve(base, (*stack, name)))
            elif base in BUILTIN_NAMES:
                profile = builtin(base)
            else:
                known = ", ".join((*BUILTIN_NAMES, *(n for n in parsed if n not in BUILTIN_NAMES)))
                raise _fail(name, None, f"없는 base다: {base!r}. 알려진 이름: {known}")
        if adopt is not None:
            profile["adopt_on_both_pass"] = adopt
        for key, override in overrides.items():
            profile["roles"][key] = merge_cell(key, profile["roles"][key], override)
        resolved[name] = profile
        return profile

    for name in parsed:
        found[name] = resolve(name, ())
    for name, profile in found.items():
        validate(name, profile)
    return found, data
