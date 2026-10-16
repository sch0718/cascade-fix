import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

DEFAULT_SENSITIVE_PATHS = ["**/auth*", "**/security*", "**/*delete*", "**/migrations/**"]
CANDIDATE_KEYS = ("tests", "full_tests", "build", "typecheck", "lint", "format")
LIST_KEYS = CANDIDATE_KEYS


class ExitCodes:
    NO_VERIFY_CONFIG = 4
    PARSE_ERROR = 5
    NEEDS_USER = 6


@dataclass
class RepoConfig:
    tests: list[str] = field(default_factory=list)
    full_tests: list[str] = field(default_factory=list)
    build: list[str] = field(default_factory=list)
    typecheck: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    format: list[str] = field(default_factory=list)
    doc_snippet_check: dict[str, str] = field(default_factory=dict)
    sensitive_paths: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVE_PATHS))


def load_repo_config(root: Path, config_path: Path) -> RepoConfig | None:
    config_path = Path(config_path)
    if not config_path.is_file():
        return None
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    entry = data.get("repos", {}).get(str(Path(root).resolve()))
    if entry is None:
        return None
    known = {f.name for f in fields(RepoConfig)}
    return RepoConfig(**{k: v for k, v in entry.items() if k in known})


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def detect_candidates(root: Path) -> dict[str, list[str]]:
    root = Path(root)
    found: dict[str, list[str]] = {k: [] for k in CANDIDATE_KEYS}

    def add(key: str, command: str) -> None:
        if command not in found[key]:
            found[key].append(command)

    text = _read_text(root / "package.json")
    if text is not None:
        try:
            scripts = json.loads(text).get("scripts", {})
        except (json.JSONDecodeError, AttributeError):
            scripts = {}
        if "test" in scripts:
            add("tests", "npm run test")
            add("full_tests", "npm run test")
        for name in ("build", "lint", "typecheck", "format"):
            if name in scripts:
                add(name, f"npm run {name}")

    text = _read_text(root / "pyproject.toml")
    if text is not None:
        try:
            tool = tomllib.loads(text).get("tool", {})
        except tomllib.TOMLDecodeError:
            tool = {}
        if "pytest" in tool or "pytest" in text:
            add("tests", "pytest {files}")
            add("full_tests", "pytest")
        if "ruff" in tool:
            add("lint", "ruff check .")
            add("format", "ruff format .")
        if "mypy" in tool:
            add("typecheck", "mypy .")

    text = _read_text(root / "Makefile")
    if text is not None:
        for target, key in (("test", "tests"), ("lint", "lint"), ("build", "build")):
            if re.search(rf"^{target}\s*:", text, re.MULTILINE):
                add(key, f"make {target}")
                if key == "tests":
                    add("full_tests", f"make {target}")

    return found
