import json
import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import replace
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

from cascade.config import ConfigError, ExitCodes, RepoConfig, detect_candidates, load_repo_config
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_UNDECIDABLE,
    VERDICT_FACT,
    VERDICT_UNDECIDED,
    Decision,
    WorkerResult,
)
from cascade.findings import Finding
from cascade.jobs import ROLES, AdjInput
from cascade.patches import DIFF_FLAGS, changed_files
from cascade.rounds import STATUS_DONE, Ledger, RoundRecord, ledger_path, needs_recheck
from cascade.snapshot import Snapshot
from cascade.verify import VerifyResult, check_doc_refs, check_snippets, related_tests

SKILL_ROOT = Path(__file__).resolve().parent.parent
RUN_REVIEW = SKILL_ROOT.parent / "codex-review" / "scripts" / "run-review.sh"
CLI = f"PYTHONPATH={shlex.quote(str(SKILL_ROOT))} python3 -m cascade.cli"
ROLE_SUFFIX = {"a": "a", "b": "b", "writer": "writer", "adjudicator": "adj"}
DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
SOURCE_DOC = "codex-doc"
SOURCE_PR = "pr-comment"
EXTERNAL_PREFIX = "외부/"
REASON_NO_PATCH = "패치 없음"
CALL_FAILED = "호출 실패"


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.message = message
        self.code = code


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CliError(f"파일이 없다: {path}") from None
    except ValueError as e:
        raise CliError(f"JSON을 읽을 수 없다({path}): {e}", ExitCodes.PARSE_ERROR) from None


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CliError(f"파일이 없다: {path}") from None


def git_toplevel(cwd: Path) -> Path:
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise CliError(f"git 저장소가 아니다: {cwd}")
    return Path(p.stdout.strip()).resolve()


def repos_toml() -> Path:
    return Path(os.environ.get("CASCADE_REPOS_TOML") or SKILL_ROOT / "repos.toml")


def load_cfg(root: Path) -> RepoConfig | None:
    path = repos_toml()
    try:
        return load_repo_config(root, path)
    except tomllib.TOMLDecodeError as e:
        raise CliError(f"저장소 설정 파일을 읽을 수 없다({path}): {e}", ExitCodes.PARSE_ERROR) from None
    except ConfigError as e:
        raise CliError(f"저장소 설정이 잘못됐다({path}): {e}", ExitCodes.PARSE_ERROR) from None


def no_verify_message(root: Path) -> str:
    return "\n".join([
        f"검증 명령이 없다. {repos_toml()} 에 [repos.\"{root}\"] 항목과 tests 명령을 적은 뒤 다시 실행하라.",
        "코드 모드는 테스트가 없는 저장소에서 돌리지 않는다.",
        "저장소에서 찾은 후보:",
        json.dumps(detect_candidates(root), ensure_ascii=False, indent=2),
    ])


def cfg_for(source: str, root: Path) -> RepoConfig:
    cfg = load_cfg(root)
    if source == SOURCE_DOC:
        return cfg or RepoConfig()
    if cfg is None or not cfg.tests:
        raise CliError(no_verify_message(root), ExitCodes.NO_VERIFY_CONFIG)
    return cfg


def load_ledger(run_dir: Path) -> Ledger:
    path = ledger_path(run_dir)
    if not path.is_file():
        raise CliError(f"원장이 없다: {path}")
    return Ledger.load(path)


def save_ledger(run_dir: Path, ledger: Ledger) -> None:
    ledger.save(ledger_path(run_dir))


def get_round(ledger: Ledger, no: int) -> RoundRecord:
    for rnd in ledger.rounds:
        if rnd.no == no:
            return rnd
    raise CliError(f"회차 {no}이 원장에 없다")


def round_dir(run_dir: Path, no: int) -> Path:
    path = Path(run_dir) / f"round-{no}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def step_done(rnd: RoundRecord, step: str) -> bool:
    return any(e.get("step") == step for e in rnd.events)


def mark(rnd: RoundRecord, step: str, **extra) -> None:
    rnd.events.append({"step": step, "at": now(), **extra})


def guard(rnd: RoundRecord, step: str, force: bool) -> None:
    if step_done(rnd, step) and not force:
        raise CliError(f"회차 {rnd.no}의 {step} 단계는 이미 했다. 다시 하려면 --force 를 붙인다")


def snapshot_of(ledger: Ledger) -> Snapshot:
    return Snapshot.from_dict(ledger.snapshot)


def wt_name(fid: str, role: str) -> str:
    return f"{fid}-{ROLE_SUFFIX[role]}"


def worktree(snap: Snapshot, fid: str, role: str) -> Path:
    name = wt_name(fid, role)
    if name not in snap.worktrees:
        raise CliError(f"워크트리가 없다: {name} (jobs·judge 단계를 먼저 한다)")
    return snap.worktrees[name]


def patch_path(rd: Path, fid: str, role: str) -> Path:
    return Path(rd) / f"patch-{fid}-{ROLE_SUFFIX[role]}.diff"


def findings_of(rnd: RoundRecord) -> list[Finding]:
    return [Finding.from_dict(f) for f in rnd.findings]


def failed_result(fid: str, role: str) -> WorkerResult:
    return WorkerResult(
        finding_id=fid, role=role, verdict=VERDICT_UNDECIDED, needs_design_decision=False,
        reason=CALL_FAILED, verify=[], checker_ok=None, model_id="",
    )


def pick(results: dict[str, WorkerResult], fid: str, role: str) -> WorkerResult:
    return results.get(f"{fid}:{role}") or failed_result(fid, role)


def worker_from(results: dict, fid: str, role: str) -> WorkerResult:
    data = results.get(f"{fid}:{role}")
    return WorkerResult.from_dict(data) if isinstance(data, dict) else failed_result(fid, role)


def model_warnings(results: dict[str, WorkerResult]) -> list[str]:
    out = []
    for key, r in results.items():
        expected = ROLES.get(r.role, (None,))[0]
        if not r.model_id and r.reason != CALL_FAILED:
            out.append(f"경고: {key} 모델 ID가 비어 있다(기대값 {expected})")
        elif r.model_id and expected and r.model_id != expected:
            out.append(f"경고: {key} 모델 ID가 {r.model_id} 이다(기대값 {expected})")
    return out


def is_sensitive(paths: list[str], cfg: RepoConfig) -> bool:
    return any(fnmatch(path, pat) for path in paths for pat in cfg.sensitive_paths)


def is_adopted(d: dict) -> bool:
    return d["outcome"] == OUTCOME_AGREED_FACT or (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from"))


def first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else "(출력 없음)"


def doc_checks(wt: Path, patch: str, ledger: Ledger, cfg: RepoConfig) -> tuple[list[str], list[VerifyResult]]:
    changed = changed_files(patch)
    docs = [p for p in changed if p in ledger.docs] or [p for p in changed if p.endswith(DOC_SUFFIXES)]
    missing: list[str] = []
    snippets: list[VerifyResult] = []
    for rel in docs:
        path = Path(wt) / rel
        if not path.is_file():
            continue
        missing += [m for m in check_doc_refs(wt, path) if m not in missing]
        snippets += check_snippets(wt, path, cfg)
    return missing, snippets


def doc_problems(missing: list[str], snippets: list[VerifyResult]) -> list[str]:
    return [*missing, *(f"코드 블록 검사 실패 {r.cmd}" for r in snippets if not r.ok)]


def doc_verify(missing: list[str], snippets: list[VerifyResult]) -> list[VerifyResult]:
    refs = VerifyResult(cmd="참조 실존", ok=not missing, tail=", ".join(missing))
    return [refs, *snippets]


def needs_checker(a: WorkerResult, b: WorkerResult, problems: list[str], writer_patch: str) -> bool:
    return bool(
        writer_patch.strip()
        and not problems
        and a.verdict == b.verdict == VERDICT_FACT
        and not a.needs_design_decision
        and not b.needs_design_decision
    )


def adj_input(ledger: Ledger, f: Finding, results: dict, verify: dict, wt: Path, cfg: RepoConfig, reasons: list[str]) -> AdjInput:
    a = worker_from(results, f.id, "a")
    b = worker_from(results, f.id, "b")
    v = verify.get(f.id, {})
    if ledger.mode == "doc":
        writer = worker_from(results, f.id, "writer")
        checker_data = results.get(f"{f.id}:checker")
        checker = WorkerResult.from_dict(checker_data) if isinstance(checker_data, dict) else None
        return AdjInput(
            finding=f, a=a, b=b, patch_a=writer.patch, patch_b="", verify_a=v.get("writer", []), verify_b=[],
            worktree=wt, mode="doc", cfg=cfg, checker=checker, escalation_reasons=list(reasons),
        )
    return AdjInput(
        finding=f, a=a, b=b, patch_a=a.patch, patch_b=b.patch, verify_a=v.get("a", []), verify_b=v.get("b", []),
        worktree=wt, mode="code", cfg=cfg, checker=None, escalation_reasons=list(reasons),
    )


def rechecks_done(ledger: Ledger, exclude: int | None = None) -> int:
    return sum(1 for r in ledger.rounds if r.recheck_opened and not r.final and r.no != exclude)


def recheck_needed(ledger: Ledger, rnd: RoundRecord) -> bool:
    if ledger.source == SOURCE_PR or not rnd.adopted:
        return False
    if rnd.final:
        return needs_recheck(replace(rnd, final=False), 1, 0)
    adopted = set(rnd.adopted)
    if any(f.get("severity") == "Critical" for f in rnd.findings if f.get("id") in adopted):
        return True
    return needs_recheck(rnd, ledger.max_rechecks, rechecks_done(ledger, exclude=rnd.no))


def base_of(ledger: Ledger, rnd: RoundRecord) -> str:
    return rnd.base_commit or ledger.snapshot.get("commit", "")


def base_name(rnd: RoundRecord) -> str:
    return "base" if rnd.no == 1 else f"base-r{rnd.no}"


def external_docs(ledger: Ledger) -> dict[str, Path]:
    return {k: Path(v) for k, v in ledger.docs.items() if k.startswith(EXTERNAL_PREFIX)}


def range_diff(root: Path, old: str, new: str) -> str:
    p = subprocess.run(["git", "-c", "core.quotepath=false", "diff", *DIFF_FLAGS, old, new],
                       cwd=root, capture_output=True)
    if p.returncode != 0:
        raise CliError(f"git diff 실패: {p.stderr.decode('utf-8', errors='replace').strip()}")
    return p.stdout.decode("utf-8", errors="replace")


def without_empty_patch(d: Decision, patches: dict[str, str], adjudicated: bool) -> Decision:
    if not is_adopted(d.to_dict()) or patches.get(d.patch_from, "").strip():
        return d
    if adjudicated or d.outcome != OUTCOME_AGREED_FACT:
        return Decision(d.finding_id, OUTCOME_UNDECIDABLE, None, [*d.reasons, REASON_NO_PATCH])
    return Decision(d.finding_id, OUTCOME_ADJUDICATE, None, [*d.reasons, REASON_NO_PATCH])


def final_suite_cfg(cfg: RepoConfig | None, mode: str, root: Path, changed: list[str]) -> tuple[RepoConfig | None, str | None]:
    if cfg is None or cfg.full_tests or mode == "doc" or not cfg.tests:
        return cfg, None
    files = " ".join(shlex.quote(f) for f in related_tests(root, changed))
    fallback = replace(cfg, full_tests=[c.replace("{files}", files) for c in cfg.tests])
    return fallback, "경고: full_tests가 없어 바뀐 파일 전체의 관련 테스트(tests 명령)로 대신 돌린다"



def renumber(items: list[Finding], round_no: int, source: str) -> list[Finding]:
    return [replace(f, id=f"r{round_no}-{i:02d}", source=source) for i, f in enumerate(items, 1)]


def unresolved_copy(f: Finding) -> Finding:
    return replace(f, body=f"[재검토: {f.id} 해소 안 됨]\n{f.body}")


def parse_remote(url: str) -> tuple[str, str]:
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    if not m:
        raise CliError(f"origin 주소에서 owner/name을 읽지 못했다: {url} (--pr-repo owner/name 으로 준다)")
    return m.group(1), m.group(2)


def _q(value) -> str:
    return shlex.quote(str(value))


def run_review_cmd(mode: str, prompt: Path, root: str, out: Path) -> str:
    return f"{_q(RUN_REVIEW)} --{mode} {_q(prompt)} --repo {_q(root)} --out {_q(out)}"


def _cmd(name: str, run_dir: Path, *rest: str) -> str:
    return " ".join([CLI, name, "--run-dir", _q(run_dir), *rest])


def next_steps(ledger: Ledger, run_dir: Path) -> list[str]:
    rnd = ledger.current_round()
    if rnd is None:
        return ["회차가 없다"]
    if ledger.status == STATUS_DONE:
        steps = []
        if not (Path(run_dir) / "report.md").is_file():
            steps.append(_cmd("report", run_dir))
        elif not any(step_done(r, "cleanup") for r in ledger.rounds):
            steps.append(_cmd("cleanup", run_dir))
        holds = [d["finding_id"] for r in ledger.rounds for d in r.decisions
                 if d["outcome"] == "보류후보" and d["finding_id"] not in ledger.hold_choices]
        if holds:
            steps.append(f"(선택, cleanup 전에) 보류 후보 {', '.join(holds)}의 선택을 받으면: " + _cmd("resume", run_dir, "--choices <JSON>"))
        return steps or ["할 일 없음"]
    rd = Path(run_dir) / f"round-{rnd.no}"
    n = f"--round {rnd.no}"
    if not step_done(rnd, "jobs"):
        return [_cmd("jobs", run_dir, n)]
    if not step_done(rnd, "judge"):
        if step_done(rnd, "judge-checker") and (rd / "checker-args.json").is_file():
            return [
                f"Workflow 실행: script=workflows/run-jobs.js 내용, args={rd / 'checker-args.json'} 내용 → 결과를 {rd / 'checker.json'} 에 저장",
                _cmd("judge", run_dir, n, f"--results {_q(rd / 'stage1.json')}", f"--checker-results {_q(rd / 'checker.json')}"),
            ]
        return [
            f"Workflow 실행: script=workflows/run-jobs.js 내용, args={rd / 'stage1-args.json'} 내용 → 결과를 {rd / 'stage1.json'} 에 저장",
            _cmd("judge", run_dir, n, f"--results {_q(rd / 'stage1.json')}"),
        ]
    if (rd / "adj-args.json").is_file() and not step_done(rnd, "adjudicate"):
        return [
            f"Workflow 실행: script=workflows/run-jobs.js 내용, args={rd / 'adj-args.json'} 내용 → 결과를 {rd / 'adj.json'} 에 저장",
            _cmd("adjudicate", run_dir, n, f"--results {_q(rd / 'adj.json')}"),
        ]
    for prompt in sorted(rd.glob("opinion-*.md")):
        fid = prompt.stem[len("opinion-"):]
        if fid not in ledger.opinions:
            out = rd / f"opinion-{fid}.out"
            return [
                run_review_cmd("opinion", prompt, ledger.root, out),
                _cmd("opinion", run_dir, n, f"--id {fid}", f"--result {_q(str(out) + '.final')}"),
            ]
    if not step_done(rnd, "apply"):
        return [_cmd("apply", run_dir, n)]
    if rnd.recheck_opened and not step_done(rnd, "recheck"):
        out = rd / "recheck.out"
        return [
            run_review_cmd("recheck", rd / "recheck-prompt.md", ledger.root, out),
            _cmd("recheck", run_dir, n, f"--result {_q(str(out) + '.final')}"),
        ]
    return [_cmd("report", run_dir)]
