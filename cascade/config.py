import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_SENSITIVE_PATHS = ["**/auth*", "**/security*", "**/*delete*", "**/migrations/**"]
CANDIDATE_KEYS = ("tests", "full_tests", "build", "typecheck", "lint", "format")
LIST_KEYS = (*CANDIDATE_KEYS, "setup", "sensitive_paths")
DICT_KEYS = ("doc_snippet_check",)


class ConfigError(ValueError):
    pass


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
    setup: list[str] = field(default_factory=list)
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
    values = {k: v for k, v in entry.items() if k in known}
    for key, value in values.items():
        if key in LIST_KEYS and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ConfigError(f"{key}는 문자열 목록이어야 한다(예: {key} = [\"...\"]): {value!r}")
        if key in DICT_KEYS and not (isinstance(value, dict) and all(isinstance(v, str) for v in value.values())):
            raise ConfigError(f"{key}는 언어별 명령 표여야 한다: {value!r}")
    return RepoConfig(**values)


def is_sensitive_path(path: str, patterns: list[str]) -> bool:
    lowered = path.lower()
    candidates = (lowered, "/" + lowered)
    return any(fnmatch(c, pat.lower()) for pat in patterns for c in candidates)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


_PYTEST_STYLE = re.compile(r"^(?:async\s+)?def test_|^class Test\w*\s*(?:\(\s*\))?\s*:", re.MULTILINE)
_UNITTEST_CASE = re.compile(r"\bTestCase\b")


def _test_runner(files: list[Path]) -> str | None:
    """test_*.py 들을 돌릴 러너. 모듈 수준 test_ 함수나 TestCase를 상속하지 않는 Test 클래스가 하나라도
    있으면 pytest(TestCase도 함께 돈다), TestCase만 있으면 unittest다. unittest.mock import 만으로는 고르지 않는다."""
    texts = [_read_text(f) or "" for f in files]
    if any(_PYTEST_STYLE.search(t) for t in texts):
        return "pytest"
    if any(_UNITTEST_CASE.search(t) for t in texts):
        return "unittest"
    return "pytest" if files else None


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
            add("format", "ruff format {files}")
        if "mypy" in tool:
            add("typecheck", "mypy .")

    text = _read_text(root / "Makefile")
    if text is not None:
        for target, key in (("test", "tests"), ("lint", "lint"), ("build", "build")):
            if re.search(rf"^{target}\s*:", text, re.MULTILINE):
                add(key, f"make {target}")
                if key == "tests":
                    add("full_tests", f"make {target}")

    if not found["tests"]:
        tests_dir = next((d for d in ("tests", "test") if (root / d).is_dir()), None)
        test_files = sorted((root / tests_dir).glob("test_*.py")) if tests_dir else []
        runner = _test_runner(test_files)
        if runner == "unittest":
            # {files}를 넣지 않는다: 파일 경로로 넘기면 discover -s 와 달리 tests_dir가 sys.path에 안 들어가
            # 테스트 안의 형제 모듈 import가 깨지고, 관련 테스트가 없으면 0건 실행(3.12부터 실패)이 된다
            add("tests", f"python3 -m unittest discover -s {tests_dir} -p 'test_*.py'")
            add("full_tests", f"python3 -m unittest discover -s {tests_dir} -p 'test_*.py'")
        elif runner == "pytest":
            # unittest discover는 pytest 함수 테스트를 세지 않는다(섞여 있으면 빠지고, 그것만 있으면
            # 0건이 3.11까지 성공으로 끝난다). 실패하는 코드가 검증을 통과하지 않게 pytest를 낸다
            add("tests", "pytest {files}")
            add("full_tests", "pytest")

    return found
