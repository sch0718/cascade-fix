import json
import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cascade.config import ConfigError, ExitCodes, RepoConfig, detect_candidates, is_sensitive_path, load_repo_config
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_CARRIED,
    OUTCOME_UNDECIDABLE,
    VERDICT_FACT,
    VERDICT_UNDECIDED,
    Decision,
    WorkerResult,
)
from cascade.findings import Finding
from cascade.jobs import ROLES, AdjInput, location_file, split_location, write_job_args
from cascade.patches import DIFF_FLAGS, changed_files, record_setup_paths
from cascade.review import REVIEWER_CLAUDE, claude_prompt, review_job
from cascade.rounds import STATUS_DONE, Ledger, RoundRecord, ledger_path, needs_recheck
from cascade.snapshot import Snapshot, add_worktree, tree_diff_stat, user_tree_fingerprint
from cascade.verify import (
    VerifyResult,
    check_snippets,
    new_broken_refs,
    related_tests,
    run_commands,
    run_worker_verifiers,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
RUN_JOBS = SKILL_ROOT / "scripts" / "run-jobs.js"
USER_REPOS_TOML = Path.home() / ".claude" / "cascade-fix" / "repos.toml"
LEGACY_REPOS_TOML = SKILL_ROOT / "repos.toml"


def find_run_review() -> Path:
    """codex-review 스킬의 run-review.sh. 플러그인 캐시에 설치돼도 개인 스킬 위치를 찾는다."""
    if os.environ.get("CASCADE_RUN_REVIEW"):
        return Path(os.environ["CASCADE_RUN_REVIEW"])
    candidates = [
        SKILL_ROOT.parent / "codex-review" / "scripts" / "run-review.sh",
        Path.home() / ".claude" / "skills" / "codex-review" / "scripts" / "run-review.sh",
    ]
    return next((c for c in candidates if c.is_file()), candidates[-1])


RUN_REVIEW = find_run_review()
CLI = f"PYTHONPATH={shlex.quote(str(SKILL_ROOT))} python3 -m cascade.cli"
ROLE_SUFFIX = {"a": "a", "b": "b", "writer": "writer", "adjudicator": "adj"}
DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
SOURCE_DOC = "codex-doc"
DOC_SOURCES = (SOURCE_DOC, "claude-doc")
SOURCE_PR = "pr-comment"
EXTERNAL_PREFIX = "외부/"
REASON_NO_PATCH = "패치 없음"
CALL_FAILED = "호출 실패"
BASELINE_TAG = "[기준선 실패·판정 제외] "
REPRO_FILE = "repro-cmds.json"
RECHECK_REPO_FILE = "recheck-repo.txt"
RECHECK_SCOPE_FILE = "recheck-scope.json"
BASELINE_FILE = "baseline.json"
OPINION_ARGS = "opinion-args.json"
OPINION_RESULTS = "opinion.json"
RECHECK_ARGS = "recheck-args.json"
RECHECK_RESULTS = "recheck.json"
FINGERPRINT_PARTS = {"tree": "작업 트리", "head": "HEAD", "stash": "stash", "branches": "브랜치"}


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


def _has_repos(path: Path) -> bool:
    try:
        return bool(tomllib.loads(path.read_text(encoding="utf-8")).get("repos"))
    except tomllib.TOMLDecodeError:
        return True  # 잘못된 파일이면 그대로 골라 오류를 보인다
    except OSError:
        return False


def repos_toml() -> Path:
    """저장소 검증 설정 파일. 환경 변수 > ~/.claude/cascade-fix/repos.toml > 스킬 폴더의 repos.toml(예전 위치, 항목이 있을 때만)."""
    if os.environ.get("CASCADE_REPOS_TOML"):
        return Path(os.environ["CASCADE_REPOS_TOML"])
    if USER_REPOS_TOML.is_file():
        return USER_REPOS_TOML
    if LEGACY_REPOS_TOML.is_file() and _has_repos(LEGACY_REPOS_TOML):
        return LEGACY_REPOS_TOML
    return USER_REPOS_TOML


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
    if source in DOC_SOURCES:
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
    return any(is_sensitive_path(path, cfg.sensitive_paths) for path in paths)


def is_adopted(d: dict) -> bool:
    return d["outcome"] == OUTCOME_AGREED_FACT or (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from"))


def first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else "(출력 없음)"


def doc_checks(
    wt: Path, base_wt: Path | None, patch: str, ledger: Ledger, cfg: RepoConfig
) -> tuple[list[str], list[VerifyResult]]:
    changed = changed_files(patch)
    docs = [p for p in changed if p in ledger.docs] or [p for p in changed if p.endswith(DOC_SUFFIXES)]
    missing: list[str] = []
    snippets: list[VerifyResult] = []
    for rel in docs:
        path = Path(wt) / rel
        if not path.is_file():
            continue
        base_doc = Path(base_wt) / rel if base_wt is not None else None
        missing += [m for m in new_broken_refs(base_wt, base_doc, wt, path) if m not in missing]
        snippets += check_snippets(wt, path, cfg)
    return missing, snippets


def map_location(location: str, root: Path, docs: dict[str, str]) -> str:
    path, line = split_location(location)
    if not path or path in ("?", "-"):
        return location
    target = Path(path)
    if target.is_absolute():
        resolved = target.resolve()
        for key, original in docs.items():
            if Path(original).resolve() == resolved:
                return f"{key}{line}"
        root = Path(root).resolve()
        return f"{resolved.relative_to(root).as_posix()}{line}" if resolved.is_relative_to(root) else location
    if path in docs:
        return location
    parts = target.parts
    matches = [key for key, original in docs.items() if Path(original).parts[-len(parts):] == parts]
    return f"{matches[0]}{line}" if len(matches) == 1 else location


def prompt_findings(ledger: Ledger, findings: list[Finding]) -> list[Finding]:
    return [replace(f, location=map_location(f.location, Path(ledger.root), ledger.docs)) for f in findings]


def new_worktree(snap: Snapshot, name: str, commit: str, cfg: RepoConfig | None) -> Path:
    path = add_worktree(snap, name, commit)
    for cmd in (cfg.setup if cfg else []):
        result = run_commands(path, [cmd.replace("{root}", shlex.quote(str(snap.root)))])[0]
        if not result.ok:
            print(f"경고: {name} 워크트리의 setup 명령이 실패했다: {cmd} — {first_line(result.tail)}")
    if cfg is not None and cfg.setup:
        record_setup_paths(path)
    return path


def worker_verify(wt: Path, cfg: RepoConfig, changed: list[str], baseline: set[str]) -> tuple[list[VerifyResult], list[VerifyResult]]:
    judged: list[VerifyResult] = []
    recorded: list[VerifyResult] = []
    for template, r in run_worker_verifiers(wt, cfg, changed):
        if template in baseline and template in baseline_excludable(cfg):
            recorded.append(VerifyResult(cmd=BASELINE_TAG + r.cmd, ok=r.ok, tail=r.tail))
            continue
        judged.append(r)
        recorded.append(r)
    return judged, recorded


def baseline_failed(rd: Path) -> set[str]:
    path = Path(rd) / BASELINE_FILE
    if not path.is_file():
        return set()
    data = read_json(path)
    return set(data.get("failed", [])) if isinstance(data, dict) else set()


def baseline_excludable(cfg: RepoConfig) -> set[str]:
    return {*cfg.build, *cfg.typecheck, *cfg.lint, *cfg.format} - set(cfg.tests)


def write_baseline(rd: Path, base_wt: Path, cfg: RepoConfig, changed: list[str]) -> list[str]:
    measured = baseline_excludable(cfg) - set(cfg.format)
    results = [(t, r) for t, r in run_worker_verifiers(base_wt, cfg, changed) if t in measured]
    failed = sorted({t for t, r in results if not r.ok})
    write_json(Path(rd) / BASELINE_FILE, {
        "failed": failed,
        "results": [{"template": t, **r.to_dict()} for t, r in results],
    })
    return failed


def last_event(rnd: RoundRecord, step: str) -> dict | None:
    return next((e for e in reversed(rnd.events) if e.get("step") == step), None)


def check_user_tree(ledger: Ledger, rnd: RoundRecord) -> None:
    event = last_event(rnd, "jobs")
    before = (event or {}).get("fingerprint")
    if not before:
        return
    snap = snapshot_of(ledger)
    now = user_tree_fingerprint(snap.root, snap.run_dir)
    changed = [k for k in FINGERPRINT_PARTS if before.get(k) != now.get(k)]
    if not changed:
        return
    lines = [
        "jobs 이후 사용자 저장소가 바뀌었다(작업자가 워크트리 밖을 건드렸을 수 있다): "
        + ", ".join(FINGERPRINT_PARTS[k] for k in changed),
    ]
    if "tree" in changed:
        lines += ["", f"git diff --stat {before['tree']} {now['tree']}", tree_diff_stat(snap.root, before["tree"], now["tree"])]
    if "head" in changed:
        lines.append(f"HEAD: {before['head']} → {now['head']}")
    if "stash" in changed:
        lines.append(f"refs/stash: {before['stash'] or '(없음)'} → {now['stash'] or '(없음)'}")
    if "branches" in changed:
        old, new = set(before.get("branches", [])), set(now.get("branches", []))
        lines += [f"- {b}" for b in sorted(old - new)] + [f"+ {b}" for b in sorted(new - old)]
    lines += ["", "사람이 확인한다. 의도한 변경이면 되돌리거나 jobs --force 로 다시 시작한다."]
    raise CliError("\n".join(lines), ExitCodes.NEEDS_USER)


def repro_listed(rd: Path) -> dict[str, list[str]]:
    path = Path(rd) / REPRO_FILE
    data = read_json(path) if path.is_file() else {}
    return data if isinstance(data, dict) else {}


def approved_repro(rd: Path, allow: str | None) -> dict[str, list[str]] | None:
    listed = repro_listed(rd)
    if not any(listed.values()):
        return None
    if allow is None:
        print(f"경고: 재현 명령을 승인받지 않아 재현 검증을 건너뛴다. 목록 {Path(rd) / REPRO_FILE} 을 사용자에게 보이고 승인받으면 --allow-repro 로 넘긴다")
        return None
    if read_json(Path(allow)) != listed:
        raise CliError(f"--allow-repro 내용이 {Path(rd) / REPRO_FILE} 와 다르다. 승인한 목록 그대로 넘긴다")
    return listed


def round_worktree_names(rnd: RoundRecord) -> list[str]:
    return [wt_name(f["id"], role) for f in rnd.findings for role in ROLE_SUFFIX]


def scope_files(rd: Path) -> set[str] | None:
    path = Path(rd) / RECHECK_SCOPE_FILE
    if not path.is_file():
        return None
    data = read_json(path)
    return set(data) if isinstance(data, list) else None


def recheck_scope(ledger: Ledger, rd: Path, adopted: list[Finding], combined: list[Finding]) -> set[str] | None:
    scope = scope_files(rd)
    if scope is None:
        return None
    root = Path(ledger.root)
    mapped = lambda f: location_file(map_location(f.location, root, ledger.docs))  # noqa: E731
    scope |= {m for f in adopted if (m := mapped(f))}
    return scope | {location_file(f.location) for f in combined if mapped(f) in scope and location_file(f.location)}


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


def range_diff(root: Path, old: str, new: str, files: list[str] | None = None) -> str:
    paths = ["--", *files] if files else []
    p = subprocess.run(["git", "--literal-pathspecs", "-c", "core.quotepath=false", "diff", *DIFF_FLAGS, old, new, *paths],
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


def carried_ids(rnd: RoundRecord, applied: list[str], failed: list[str], patches: dict[str, str]) -> set[str]:
    if rnd.final:
        return set()
    carried_before = {f["id"] for f in rnd.findings if f.get("carried_from")}
    applied_files = {p for fid in applied for p in changed_files(patches.get(fid, ""))}
    return {fid for fid in failed
            if fid not in carried_before and applied_files & set(changed_files(patches.get(fid, "")))}


def carried_findings(rnd: RoundRecord) -> list[Finding]:
    ids = {d["finding_id"] for d in rnd.decisions if d["outcome"] == OUTCOME_CARRIED}
    return [replace(f, carried_from=f.id) for f in findings_of(rnd) if f.id in ids]


def is_claude(ledger: Ledger) -> bool:
    return ledger.reviewer == REVIEWER_CLAUDE


def opinion_job_id(fid: str) -> str:
    return f"{fid}:opinion"


def recheck_job_id(no: int) -> str:
    return f"round-{no}:recheck"


def write_review_args(rd: Path, name: str, ledger: Ledger, items: list[tuple[str, str, str, str]]) -> Path:
    """items: (job id, 표시 이름, 프롬프트, 참고 저장소). Claude 검토자 Workflow 입력을 rd/name 에 쓴다."""
    jobs = [review_job(job_id, text, claude_prompt(prompt, repo), ledger.review_model, ledger.review_effort)
            for job_id, text, prompt, repo in items]
    out = Path(rd) / name
    write_json(out, write_job_args({"jobs": jobs}, Path(rd) / "prompts"))
    return out


def pending_opinions(ledger: Ledger, rd: Path) -> list[str]:
    return [fid for p in sorted(Path(rd).glob("opinion-*.md")) if (fid := p.stem[len("opinion-"):]) not in ledger.opinions]


def workflow_step(args: Path, out: Path) -> str:
    return f"Workflow 실행: script={RUN_JOBS} 내용, args={args} 내용 → 결과를 {out} 에 저장"


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
        latest = next((e for e in reversed(rnd.events) if e.get("step") != "cleanup"), {})
        reported = latest.get("step") == "report" and latest.get("round") == rnd.no
        if not reported or not (Path(run_dir) / "report.md").is_file():
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
                workflow_step(rd / "checker-args.json", rd / "checker.json"),
                _cmd("judge", run_dir, n, f"--results {_q(rd / 'stage1.json')}", f"--checker-results {_q(rd / 'checker.json')}"),
            ]
        steps = [
            workflow_step(rd / "stage1-args.json", rd / "stage1.json"),
            _cmd("judge", run_dir, n, f"--results {_q(rd / 'stage1.json')}"),
        ]
        if any(repro_listed(rd).values()):
            steps.append(f"(재현 명령 {rd / REPRO_FILE} 을 사용자에게 보이고 승인받았으면 judge에 --allow-repro {_q(rd / REPRO_FILE)} 를 붙인다)")
        return steps
    if (rd / "adj-args.json").is_file() and not step_done(rnd, "adjudicate"):
        return [
            workflow_step(rd / "adj-args.json", rd / "adj.json"),
            _cmd("adjudicate", run_dir, n, f"--results {_q(rd / 'adj.json')}"),
        ]
    pending = pending_opinions(ledger, rd)
    if pending and is_claude(ledger):
        return [
            workflow_step(rd / OPINION_ARGS, rd / OPINION_RESULTS),
            _cmd("opinion", run_dir, n, f"--results {_q(rd / OPINION_RESULTS)}"),
        ]
    if pending:
        fid = pending[0]
        out = rd / f"opinion-{fid}.out"
        return [
            run_review_cmd("opinion", rd / f"opinion-{fid}.md", ledger.root, out),
            _cmd("opinion", run_dir, n, f"--id {fid}", f"--result {_q(str(out) + '.final')}"),
        ]
    if not step_done(rnd, "apply"):
        return [_cmd("apply", run_dir, n)]
    if rnd.recheck_opened and not step_done(rnd, "recheck") and is_claude(ledger):
        return [
            workflow_step(rd / RECHECK_ARGS, rd / RECHECK_RESULTS),
            _cmd("recheck", run_dir, n, f"--result {_q(rd / RECHECK_RESULTS)}"),
        ]
    if rnd.recheck_opened and not step_done(rnd, "recheck"):
        out = rd / "recheck.out"
        repo_file = rd / RECHECK_REPO_FILE
        repo = repo_file.read_text(encoding="utf-8").strip() if repo_file.is_file() else ledger.root
        return [
            run_review_cmd("recheck", rd / "recheck-prompt.md", repo, out),
            _cmd("recheck", run_dir, n, f"--result {_q(str(out) + '.final')}"),
        ]
    return [_cmd("report", run_dir)]
