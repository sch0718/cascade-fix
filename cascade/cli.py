import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from cascade import assignment
from cascade import cli_steps as s
from cascade import profiles
from cascade.config import ConfigError, ExitCodes
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_CARRIED,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
    REASON_SENSITIVE,
    VERDICT_FACT,
    Decision,
    after_adjudication,
    decide,
    decide_doc,
)
from cascade.detect import detect, estimate_line
from cascade.detect import render as render_detect
from cascade.findings import (
    SEVERITIES,
    Finding,
    ParseError,
    fetch_pr_findings,
    parse_codex_code,
    parse_codex_doc,
    write_findings,
)
from cascade.jobs import (
    build_adjudicator_jobs,
    build_checker_jobs,
    build_stage1_jobs,
    location_file,
    read_results,
    split_job_args,
    write_job_args,
)
from cascade.patches import ApplyReport, apply_all, changed_files, extract
from cascade.report import render, write_prepared
from cascade.review import (
    CALL_FAILED_NOTE,
    CLAUDE_SOURCES,
    CURSOR_SOURCES,
    DEFAULT_EFFORT,
    DEFAULT_MODEL,
    EFFORTS,
    REVIEW_JOB,
    REVIEWER_AUTO,
    REVIEWER_CHOICES,
    REVIEWER_CLAUDE,
    REVIEWER_CODEX,
    REVIEWER_CURSOR,
    build_review_prompt,
    claude_prompt,
    label,
    load_text,
    model_warning,
    read_review_results,
    review_job,
)
from cascade.rounds import (
    RESOLUTION_HEADER,
    STATUS_DONE,
    STATUS_RUNNING,
    Ledger,
    RoundRecord,
    build_opinion_prompt,
    build_recheck_prompt,
    classify_recheck,
    out_of_scope,
    parse_recheck,
)
from cascade.runners import (
    RUNNER_CODEX,
    RUNNER_CURSOR,
    STATUS_OK,
    CliJob,
    CodexRunner,
    CursorRunner,
    codex_available,
    codex_model,
    cursor_available,
    find_codex,
    find_cursor_agent,
    run_all,
)
from cascade.sandbox_check import ensure as ensure_sandbox
from cascade.snapshot import (
    RUN_DIR_NAME,
    add_worktree,
    cleanup,
    commit_worktree,
    create_snapshot,
    ensure_excluded,
    remove_worktree,
    snapshot_tree,
    user_tree_fingerprint,
)
from cascade.verify import repro_cmds, run_repro, run_verifiers
from cascade.workers import (
    DEFAULT_CURSOR_MODEL,
    WORKERS_CHOICES,
    WORKERS_CLAUDE,
    WORKERS_EXTERNAL,
    describe,
    fallback_counts,
    has_external,
    usage_totals,
)

SOURCES = ("codex-doc", "codex-code", "claude-doc", "claude-code", "cursor-doc", "cursor-code", "pr-comment")
HOLD_CHOICES = ("지금 반영", "구현 때 테스트로", "보류")
CHOICE_NOW = "지금 반영"
REASON_APPLY_FAILED = "패치 적용 실패"
REASON_CARRIED = "앞서 적용한 패치와 겹쳐 다음 회차에서 바뀐 코드로 다시 고친다"
REASON_INTERFERENCE = "전체 테스트 실패(간섭 의심)"
REASON_FINAL_BASELINE_FAILED = "전체 테스트 실패(기준선도 실패)"
REASON_OUT_OF_SCOPE = "재검토 범위 밖"
REASON_SEVERITY_DEFERRED = "등급 선택(--severity)으로 이번 실행에서 뺌"
RAW_PREVIEW_LINES = 5
FINAL_TAIL_CHARS = 2000


def _print_next(ledger: Ledger, run_dir: Path) -> None:
    print("다음:")
    for line in s.next_steps(ledger, run_dir):
        print(f"  {line}")


def _set_decision(rnd: RoundRecord, fid: str, outcome: str, reason: str) -> None:
    for d in rnd.decisions:
        if d["finding_id"] == fid:
            if s.is_adopted(d) and not d.get("adopted_as"):
                d["adopted_as"] = d["outcome"]
            d.update(outcome=outcome, patch_from=None, reasons=[*d.get("reasons", []), reason])


def _new_run_id(root: Path) -> str:
    base = f"cf-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_id, n = base, 2
    while (root / RUN_DIR_NAME / run_id).exists():
        run_id, n = f"{base}-{n}", n + 1
    return run_id


def _split_docs(root: Path, docs: list[str]) -> tuple[dict[str, str], list[Path]]:
    names: dict[str, str] = {}
    external: list[Path] = []
    for raw in docs:
        path = Path(raw).resolve()
        if not path.is_file():
            raise s.CliError(f"문서가 없다: {raw}")
        if path.is_relative_to(root):
            rel = path.relative_to(root).as_posix()
            ignored = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root).returncode == 0
            if not ignored:
                names[rel] = str(path)
                continue
        external.append(path)
        names[f"{s.EXTERNAL_PREFIX}{path.name}"] = str(path)
    return names, external


FINDING_REQUIRED = ("id", "severity", "location", "body")
FINDING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _load_findings_json(path: str, source: str) -> list[Finding]:
    data = s.read_json(Path(path))
    if not isinstance(data, list):
        raise s.CliError(f"--findings-json은 지적 객체의 목록이다: {path}", ExitCodes.PARSE_ERROR)
    findings = []
    for i, item in enumerate(data, 1):
        missing = [k for k in FINDING_REQUIRED if not isinstance(item, dict) or k not in item]
        if missing:
            raise s.CliError(f"--findings-json {i}번째 지적에 {', '.join(missing)} 가 없다", ExitCodes.PARSE_ERROR)
        if not isinstance(item["id"], str) or not FINDING_ID.match(item["id"]):
            raise s.CliError(f"--findings-json {i}번째 지적의 id가 형식에 맞지 않는다: {item['id']!r}", ExitCodes.PARSE_ERROR)
        findings.append(Finding.from_dict({"source": source, "evidence": "", **item}))
    ids = [f.id for f in findings]
    duplicated = sorted({fid for fid in ids if ids.count(fid) > 1})
    if duplicated:
        raise s.CliError(f"--findings-json에 id가 겹친다: {', '.join(duplicated)}", ExitCodes.PARSE_ERROR)
    return findings


def _warn_pr_head(root: Path, gh: list[str], owner: str, name: str, number: str) -> None:
    p = subprocess.run([*gh, "pr", "view", number, "-R", f"{owner}/{name}", "--json", "headRefOid", "--jq", ".headRefOid"],
                       capture_output=True, text=True)
    pr_head = p.stdout.strip() if p.returncode == 0 else ""
    local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    if not pr_head:
        print(f"경고: PR #{number}의 head 커밋을 확인하지 못했다. 로컬 HEAD가 PR 브랜치인지 직접 확인한다")
    elif pr_head != local:
        print(f"경고: 로컬 HEAD({local[:12]})가 PR #{number}의 head({pr_head[:12]})와 다르다. PR 브랜치를 받아 온 뒤 실행한다")


def _parse_input(args, root: Path):
    if args.findings_json:
        return _load_findings_json(args.findings_json, args.source), ""
    if not args.input:
        raise s.CliError("--input 또는 --findings-json 중 하나를 준다")
    if args.source == "pr-comment":
        if args.pr_repo:
            owner, _, name = args.pr_repo.partition("/")
        else:
            url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root, capture_output=True, text=True)
            owner, name = s.parse_remote(url.stdout)
        gh = shlex.split(os.environ.get("CASCADE_GH", "gh"))
        me = args.me
        if me is None:
            p = subprocess.run([*gh, "api", "user", "--jq", ".login"], capture_output=True, text=True)
            me = p.stdout.strip() if p.returncode == 0 else ""
            if not me:
                print("경고: 내 계정을 알 수 없어 본인 코멘트를 거르지 못한다(--me 로 준다)")
        if not args.input.isdigit():
            raise s.CliError(f"pr-comment의 --input은 PR 번호다: {args.input}")
        _warn_pr_head(root, gh, owner, name, args.input)
        return fetch_pr_findings(owner, name, int(args.input), me, gh, 1), ""
    if args.source in CLAUDE_SOURCES:
        return _parse_claude_review(args)
    raw = s.read_text(Path(args.input))
    try:
        if args.source in CURSOR_SOURCES:
            return [replace(f, source=args.source) for f in parse_codex_doc(raw, 1)], raw
        parser = parse_codex_doc if args.source == "codex-doc" else parse_codex_code
        return parser(raw, 1), raw
    except ParseError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None


def _parse_claude_review(args):
    path = Path(args.input)
    if not path.is_file():
        raise s.CliError(f"파일이 없다: {path}")
    raw, model_id = load_text(path, REVIEW_JOB if path.suffix == ".json" else None)
    if not raw.strip():
        raise s.CliError(f"Claude 검토 결과가 비어 있다(Workflow 호출 실패일 수 있다): {path}", ExitCodes.PARSE_ERROR)
    warning = model_warning(REVIEW_JOB, model_id, args.review_model or DEFAULT_MODEL)
    if warning:
        print(warning)
    try:
        findings = parse_codex_doc(raw, 1)
    except ParseError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None
    return [replace(f, source=args.source) for f in findings], raw


def _probe(available: dict[str, bool], runners) -> dict[str, bool]:
    for runner in runners:
        if runner not in available:
            available[runner] = codex_available() if runner == RUNNER_CODEX else cursor_available()
    return available


def _cursor_reviewer_ok(available: dict[str, bool]):
    return lambda: s.CURSOR_REVIEW.is_file() and _probe(available, (RUNNER_CURSOR,))[RUNNER_CURSOR]


def _resolve_profile(args, root: Path, available: dict[str, bool]) -> profiles.Resolved:
    env = os.environ
    if WORKERS_EXTERNAL in (args.workers, env.get("CASCADE_WORKERS")):
        _probe(available, (RUNNER_CODEX, RUNNER_CURSOR))
    cli = profiles.LegacyOptions(
        workers=args.workers, cursor_model=args.cursor_model, reviewer=args.reviewer,
        review_model=getattr(args, "review_model", None), review_effort=getattr(args, "review_effort", None),
        adopt_on_both_pass=getattr(args, "adopt_on_both_pass", False))
    try:
        resolved = profiles.resolve(root, s.repos_toml(), flag=args.profile, cli=cli, env=env, available=available,
                                    codex_model=codex_model(Path.home() / ".codex"))
    except ConfigError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None
    _probe(available, assignment.runners_to_check(resolved.roles, resolved.overrides))
    return resolved


def _decide_roles(args, resolved: profiles.Resolved, source: str, available: dict[str, bool]) -> tuple[dict, list[dict]]:
    try:
        return assignment.decide(resolved.roles, resolved.overrides, source, s.RUN_REVIEW, _cursor_reviewer_ok(available),
                                 available, cursor_model=args.cursor_model or DEFAULT_CURSOR_MODEL, name=resolved.name)
    except profiles.ProfileError as e:
        raise s.CliError(str(profiles.explain_overrides(e, resolved.overrides)), ExitCodes.PARSE_ERROR) from None


def _consent_option(raw: str | None) -> set[str] | None:
    if raw is None:
        return None
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if names == ["none"]:
        return set()
    if not names or any(name not in assignment.EXTERNAL_RUNNERS for name in names):
        raise s.CliError(f"--consent는 {','.join(assignment.EXTERNAL_RUNNERS)} 중에서 쉼표로 고르거나 none이다: {raw}")
    return set(names)


def cmd_init(args) -> int:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    s.cfg_for(args.source, root)
    consent = _consent_option(args.consent)
    available: dict[str, bool] = {}
    resolved = _resolve_profile(args, root, available)
    findings, raw = _parse_input(args, root)
    if not findings:
        print("지적 0건 — 원문을 확인하라")
        for line in raw.splitlines()[:RAW_PREVIEW_LINES]:
            print(f"  | {line}")
        if "[P" in raw:
            print("경고: 원문에 「[P」가 있다. 파서가 항목을 놓쳤을 수 있다")
        print(f"status={STATUS_DONE}")
        return 0
    keep = _severity_filter(args.severity)
    deferred = [f for f in findings if f.severity not in keep]
    findings = [f for f in findings if f.severity in keep]
    if not findings:
        raise s.CliError(f"--severity {args.severity} 에 맞는 지적이 없다(전체 {len(deferred)}건). 등급을 넓힌다")
    docs, external = _split_docs(root, args.doc or [])
    mode = "doc" if args.source in s.DOC_SOURCES else "code"
    roles, substituted = _decide_roles(args, resolved, args.source, available)
    roles = assignment.fill_codex_model(roles, codex_model(Path.home() / ".codex"))
    if consent is None:
        consented = assignment.used_recipients(roles, mode)
    else:
        roles, dropped = assignment.apply_consent(roles, consent)
        substituted, consented = [*substituted, *dropped], sorted(consent)
    record = {"name": resolved.name, "source": resolved.source, "explicit": resolved.explicit, "roles": roles,
              "overrides": resolved.overrides, "substituted": substituted, "consented": consented, "first_review": {}}
    requested = args.workers or (None if resolved.explicit else os.environ.get("CASCADE_WORKERS") or None)
    legacy = assignment.legacy_fields({**record, "adopt_on_both_pass": resolved.adopt_on_both_pass}, mode, requested)
    reviewer, workers, workers_choice = legacy["reviewer"], legacy["workers"], legacy["workers_choice"]
    ensure_excluded(root)
    snap = create_snapshot(root, _new_run_id(root), external)
    stamp = s.now()
    ledger = Ledger(
        run_id=snap.run_id,
        source=args.source,
        mode=mode,
        root=str(root),
        snapshot=snap.to_dict(),
        dry_run=args.dry_run,
        max_rechecks=args.max_rechecks,
        rounds=[RoundRecord(no=1, findings=[f.to_dict() for f in findings],
                            decisions=[Decision(f.id, OUTCOME_HOLD, None, [REASON_SEVERITY_DEFERRED]).to_dict() for f in deferred],
                            adopted=[], recheck_opened=False, started_at=stamp, events=[{"step": "init", "at": stamp}],
                            base_commit=snap.commit, hold_findings=[f.to_dict() for f in deferred])],
        docs=docs,
        reviewer=reviewer,
        review_model=legacy["review_model"],
        review_effort=legacy["review_effort"],
        workers_choice=workers_choice,
        workers=workers,
        adopt_on_both_pass=legacy["adopt_on_both_pass"],
        profile=record,
    )
    write_findings(s.round_dir(snap.run_dir, 1) / "findings.json", findings)
    s.save_ledger(snap.run_dir, ledger)
    counts = Counter(f.severity for f in findings)
    print(f"run_dir={snap.run_dir}")
    print(f"status={STATUS_RUNNING}")
    print(f"지적 {len(findings)}건: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if deferred:
        print(f"등급 선택으로 뺀 지적 {len(deferred)}건은 보류 후보로 둔다(나중에 resume 「지금 반영」으로 돌릴 수 있다)")
    _print_assignment(resolved, roles, substituted, mode)
    print(f"규모: {estimate_line(len(findings), ledger.mode, reviewer, workers)}")
    if workers_choice == WORKERS_EXTERNAL:
        if has_external(workers):
            print(f"작업자: {describe(workers)}")
        else:
            print("작업자: 쓸 수 있는 외부 CLI가 없다. 모두 Claude로 돈다(--workers claude 와 같다)")
    if args.dry_run:
        print("dry-run: 채택 패치를 작업 트리에 적용하지 않는다")
    if ledger.adopt_on_both_pass:
        print("한쪽 채택: 같은 판정에 패치가 달라도 둘 다 검증을 통과하면 작은 패치를 채택한다(판가름 안 함)")
    if reviewer == REVIEWER_CLAUDE:
        print(f"의견·재검토: Claude 단독({ledger.review_model}, {ledger.review_effort}) — 외부 전송 없음")
    else:
        script = s.review_script(ledger)
        print(f"의견·재검토: {label(reviewer)}({script})")
        if not script.is_file():
            skill = "cursor-review" if reviewer == REVIEWER_CURSOR else "codex-review"
            print(f"경고: {skill} 스킬이 없다. Claude 단독으로 돌리려면 --reviewer claude 로 다시 init 한다")
    _print_next(ledger, snap.run_dir)
    return 0


def _print_assignment(resolved: profiles.Resolved, roles: dict, substituted: list[dict], mode: str) -> None:
    print(f"프로필: {resolved.name} ({resolved.source})")
    for notice in resolved.notices:
        print(f"참고: {notice}")
    if resolved.overrides:
        print(assignment.overrides_line(resolved.overrides))
    for record in substituted:
        if record["role"].startswith(f"{mode}."):
            print(f"대체: {record['role']} {record['from']} → {record['to']} — {record['reason']}")
    print(assignment.roles_line(roles, mode))
    consent = assignment.consent_line(roles, mode)
    if consent:
        print(consent)


def _severity_filter(raw: str | None) -> set[str]:
    if not raw:
        return set(SEVERITIES)
    chosen = {x.strip().capitalize() for x in raw.split(",") if x.strip()}
    unknown = chosen - set(SEVERITIES)
    if unknown or not chosen:
        raise s.CliError(f"--severity 는 {', '.join(SEVERITIES)} 중에서 쉼표로 고른다: {raw}")
    return chosen


def _root_flag(args, root: Path) -> str:
    """--root 로 다른 저장소를 가리켰으면 다음 명령에도 붙인다(빼면 현재 디렉터리의 저장소가 대상이 된다)."""
    return f" --root {shlex.quote(str(root))}" if args.root else ""


def cmd_detect(args) -> int:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    gh = None if args.no_gh else shlex.split(os.environ.get("CASCADE_GH", "gh"))
    available: dict[str, bool] = {}
    resolved = _resolve_profile(args, root, available)
    env = {} if resolved.explicit else os.environ
    reviewer_choice = args.reviewer or env.get("CASCADE_REVIEWER") or REVIEWER_AUTO
    workers_choice = args.workers or env.get("CASCADE_WORKERS") or WORKERS_CLAUDE
    cursor_model = args.cursor_model or DEFAULT_CURSOR_MODEL
    try:
        result = detect(root, repos_toml=s.repos_toml(), run_review=s.RUN_REVIEW, cursor_ok=_cursor_reviewer_ok(available), cli=s.CLI, gh=gh,
                        input_path=Path(args.input).resolve() if args.input else None, pr=args.pr,
                        reviewer_choice=reviewer_choice, root_flag=_root_flag(args, root),
                        docs=[Path(d).resolve() for d in args.doc or []], cwd=Path.cwd(),
                        workers_choice=workers_choice, cursor_model=cursor_model,
                        profile=resolved, available=available, profile_flag=args.profile)
    except profiles.ProfileError as e:
        raise s.CliError(str(profiles.explain_overrides(e, resolved.overrides)), ExitCodes.PARSE_ERROR) from None
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render_detect(result))
    return 0 if result["ok"] else ExitCodes.NEEDS_USER


def cmd_review(args) -> int:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    if args.kind == "doc":
        if not args.doc:
            raise s.CliError("문서 검토는 --doc <문서> 를 하나 이상 준다")
        docs = []
        for raw in args.doc:
            path = Path(raw).resolve()
            if not path.is_file():
                raise s.CliError(f"문서가 없다: {raw}")
            docs.append(path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path))
        prompt = build_review_prompt("doc", root, docs=docs, focus=args.focus or "")
    else:
        if sum(bool(x) for x in (args.base, args.uncommitted, args.commit)) != 1:
            raise s.CliError("코드 검토는 --base <브랜치> / --uncommitted / --commit <SHA> 중 하나를 준다")
        prompt = build_review_prompt("code", root, base=args.base, uncommitted=args.uncommitted,
                                     commit=args.commit, focus=args.focus or "")
    ensure_excluded(root)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir, n = root / RUN_DIR_NAME / f"review-{stamp}", 2
    while out_dir.exists():
        out_dir, n = root / RUN_DIR_NAME / f"review-{stamp}-{n}", n + 1
    model, effort = args.review_model or DEFAULT_MODEL, args.review_effort or DEFAULT_EFFORT
    job = review_job(REVIEW_JOB, f"{args.kind} review", claude_prompt(prompt, root), model, effort)
    args_path = out_dir / "review-args.json"
    s.write_json(args_path, write_job_args({"jobs": [job]}, out_dir / "prompts"))
    result = out_dir / "review.json"
    source = "claude-doc" if args.kind == "doc" else "claude-code"
    docs_args = "".join(f" --doc {shlex.quote(str(Path(d).resolve()))}" for d in args.doc or [])
    print(f"review_dir={out_dir}")
    print(f"검토자: Claude({model}, {effort})")
    print("다음:")
    print(f"  {s.workflow_step(args_path, result)}")
    print(f"  {s.CLI} init --source {source} --input {shlex.quote(str(result))}{docs_args}"
          + (f" --review-model {shlex.quote(model)}" if args.review_model else "")
          + (f" --review-effort {effort}" if args.review_effort else "")
          + _root_flag(args, root))
    return 0


def cmd_jobs(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "jobs", args.force)
    cfg = s.cfg_for(ledger.source, Path(ledger.root))
    snap = s.snapshot_of(ledger)
    base = s.base_of(ledger, rnd)
    base_wt = snap.worktrees.get(s.base_name(rnd)) or s.new_worktree(snap, s.base_name(rnd), base, cfg)
    roles = ("a", "b", "writer") if ledger.mode == "doc" else ("a", "b")
    findings = s.prompt_findings(ledger, s.findings_of(rnd))
    wts: dict[str, dict[str, Path]] = {}
    for f in findings:
        wts[f.id] = {}
        for role in roles:
            name = s.wt_name(f.id, role)
            wts[f.id][role] = snap.worktrees[name] if name in snap.worktrees else s.new_worktree(snap, name, base, cfg)
    rd = s.round_dir(run_dir, rnd.no)
    failed: list[str] = []
    if ledger.mode == "code" and cfg.tests:
        located = list(dict.fromkeys(p for f in findings if (p := location_file(f.location))))
        failed = s.write_baseline(rd, base_wt, cfg, located)
        if failed:
            print(f"기준선에서 이미 실패하는 검증 명령(판정에서 제외): {', '.join(failed)}")
    if ledger.mode == "code" and has_external(ledger.workers):
        ledger.workers, notices = ensure_sandbox(Path(ledger.root), cfg, base_wt, ledger.workers, failed)
        for notice in notices:
            print(notice)
        if notices:
            s.save_ledger(run_dir, ledger)
    if ledger.source == s.SOURCE_PR:
        listed = {f.id: repro_cmds(f) for f in findings}
        s.write_json(rd / s.REPRO_FILE, listed)
        if any(listed.values()):
            print(f"재현 명령(승인 전에는 돌리지 않는다): {rd / s.REPRO_FILE}")
            for fid, cmds in listed.items():
                for cmd in cmds:
                    print(f"  {fid}: {cmd}")
    out = rd / "stage1-args.json"
    claude_args, cli_args = split_job_args(
        build_stage1_jobs(findings, cfg, wts, ledger.mode, ledger.workers or None), rd / "prompts")
    s.write_json(out, claude_args)
    cli_out = rd / "stage1-cli-args.json"
    had_cli = cli_out.is_file()
    if cli_args["jobs"]:
        s.write_json(cli_out, cli_args)
    else:
        cli_out.unlink(missing_ok=True)
    s.clear_cli_outputs(rd, s.STAGE1, s.STAGE_CHECKER)
    if cli_args["jobs"] or had_cli:
        for stage in (s.STAGE1, s.STAGE_CHECKER):
            (rd / s.STAGE_OUTPUTS[stage]).unlink(missing_ok=True)
    ledger.snapshot = snap.to_dict()
    fingerprint = user_tree_fingerprint(snap.root, snap.run_dir)
    s.mark(rnd, "jobs", worktrees={fid: {r: str(p) for r, p in m.items()} for fid, m in wts.items()},
           fingerprint=fingerprint)
    s.save_ledger(run_dir, ledger)
    print(f"작업 {len(claude_args['jobs'])}건: {out}")
    if cli_args["jobs"]:
        print(f"CLI 작업 {len(cli_args['jobs'])}건: {cli_out}")
    _print_next(ledger, run_dir)
    return 0


def _cli_job(entry: dict) -> CliJob:
    return CliJob(entry["id"], entry["role"], entry["kind"], entry["runner"], entry["model"], entry["effort"],
                  Path(entry["promptPath"]), Path(entry["worktree"]), entry["writable"])


_CLI_MAKERS = {RUNNER_CODEX: (find_codex, CodexRunner), RUNNER_CURSOR: (find_cursor_agent, CursorRunner)}


def _cli_runners(jobs: list[CliJob]) -> dict[str, object]:
    runners = {}
    for name in {job.runner for job in jobs} & _CLI_MAKERS.keys():
        find, make = _CLI_MAKERS[name]
        exe = find()
        if exe:
            runners[name] = make(exe)
    return runners


def cmd_workers(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    step = f"workers-{args.stage}"
    if not s.step_done(rnd, "jobs"):
        raise s.CliError(f"회차 {rnd.no}의 jobs 단계를 먼저 한다")
    if s.judged_after_workers_input(rnd, args.stage):
        raise s.CliError(f"회차 {rnd.no}의 judge를 이미 했다. 작업자 결과를 다시 만들려면 jobs --force로 회차를 다시 연다")
    if s.workers_done(rnd, args.stage) and not args.force:
        raise s.CliError(f"회차 {rnd.no}의 {step} 단계는 이미 했다. 다시 하려면 --force 를 붙인다")
    if args.stage == s.STAGE_CHECKER and not s.checker_pending(rnd):
        raise s.CliError(f"회차 {rnd.no}의 judge 단계를 먼저 한다(1차 호출이 점검자 작업을 만든다)")
    rd = s.round_dir(run_dir, rnd.no)
    cli_args = rd / f"{args.stage}-cli-args.json"
    if not cli_args.is_file():
        raise s.CliError(f"CLI 작업이 없다: {cli_args.name} 가 없다(jobs 단계가 CLI 작업을 만든 회차에서만 쓴다)")
    entries = s.read_json(cli_args)["jobs"]
    jobs = [_cli_job(e) for e in entries]
    outcomes = run_all(jobs, _cli_runners(jobs))
    out = rd / f"{args.stage}-cli.json"
    s.write_json(out, {"results": [{"id": o.id, "output": o.output} for o in outcomes],
                       "outcomes": [o.to_dict() for o in outcomes]})
    failed = [o.id for o in outcomes if o.status != STATUS_OK]
    fallback_args = rd / f"{args.stage}-fallback-args.json"
    (rd / f"{args.stage}-fallback.json").unlink(missing_ok=True)
    if args.stage == s.STAGE1:
        s.clear_cli_outputs(rd, s.STAGE_CHECKER)
    if failed:
        s.write_json(fallback_args, {"jobs": [s.fallback_job(e) for e in entries if e["id"] in failed]})
    else:
        fallback_args.unlink(missing_ok=True)
    counts = dict(Counter(o.status for o in outcomes))
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.mark(rnd, step, counts=counts, usage=usage_totals(outcomes), fallback=failed)
    s.save_ledger(run_dir, ledger)
    n, reasons = fallback_counts(counts)
    print(f"CLI 작업 {len(outcomes)}건: 성공 {counts.get(STATUS_OK, 0)}, 대체 실행 {n}" + (f"({reasons})" if n else ""))
    print(f"결과: {out}")
    _print_next(ledger, run_dir)
    return 0


def _judge_code(ledger, rnd, f, results, snap, cfg, baseline, repro):
    wt_a, wt_b = s.worktree(snap, f.id, "a"), s.worktree(snap, f.id, "b")
    a, b = s.pick(results, f.id, "a"), s.pick(results, f.id, "b")
    base = s.base_of(ledger, rnd)
    pa, pb = extract(wt_a, base, cfg), extract(wt_b, base, cfg)
    a.patch, b.patch = pa, pb
    if a.verdict != VERDICT_FACT and b.verdict != VERDICT_FACT and not pa.strip() and not pb.strip():
        va, vb, ra, rb = [], [], [], []
    else:
        va, ra = s.worker_verify(wt_a, cfg, changed_files(pa), baseline)
        vb, rb = s.worker_verify(wt_b, cfg, changed_files(pb), baseline)
        if repro and repro.get(f.id):
            base_wt = snap.worktrees[s.base_name(rnd)]
            for wt, judged, recorded in ((wt_a, va, ra), (wt_b, vb, rb)):
                extra = run_repro(base_wt, wt, repro[f.id])
                judged += extra
                recorded += extra
    sensitive = s.is_sensitive(changed_files(pa) + changed_files(pb), cfg)
    d = decide(a, b, va, vb, pa, pb, sensitive, ledger.adopt_on_both_pass)
    d = s.without_empty_patch(d, {"a": pa, "b": pb}, adjudicated=False)
    workers = {"a": a, "b": b}
    return d, workers, {"a": [r.to_dict() for r in ra], "b": [r.to_dict() for r in rb]}


def _judge_doc_inputs(ledger, rnd, f, results, snap, cfg):
    wt = s.worktree(snap, f.id, "writer")
    a, b, writer = (s.pick(results, f.id, r) for r in ("a", "b", "writer"))
    writer.patch = extract(wt, s.base_of(ledger, rnd), cfg)
    missing, snippets = s.doc_checks(wt, snap.worktrees.get(s.base_name(rnd)), writer.patch, ledger, cfg)
    return {"a": a, "b": b, "writer": writer}, s.doc_problems(missing, snippets), [r.to_dict() for r in snippets]


def cmd_judge(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "judge", args.force)
    if not s.step_done(rnd, "jobs"):
        raise s.CliError(f"회차 {rnd.no}의 jobs 단계를 먼저 한다")
    s.check_user_tree(ledger, rnd)
    cfg = s.cfg_for(ledger.source, Path(ledger.root))
    snap = s.snapshot_of(ledger)
    rd = s.round_dir(run_dir, rnd.no)
    stage1_json, loaded_ids = s.stage_inputs(run_dir, rnd, rd, s.STAGE1, args.results, "--results")
    results = read_results(stage1_json)
    checker_results = None
    if args.checker_results or ((rd / "checker-cli-args.json").is_file() and s.checker_pending(rnd)):
        checker_json, checker_ids = s.stage_inputs(run_dir, rnd, rd, s.STAGE_CHECKER, args.checker_results, "--checker-results")
        checker_results = read_results(checker_json)
        loaded_ids |= checker_ids
    baseline = s.baseline_failed(rd)
    repro = s.approved_repro(rd, args.allow_repro) if ledger.source == s.SOURCE_PR else None
    judged = {**results, **(checker_results or {})}
    for warning in s.model_warnings(judged, {**s.expected_model_ids(ledger, judged), **loaded_ids}):
        print(warning)
    findings = s.findings_of(rnd)
    merged: dict[str, dict] = {}
    verify: dict[str, dict] = {}
    decisions: list[Decision] = []
    pending: list = []
    writer_patches: dict[str, str] = {}
    doc_problems: dict[str, list[str]] = {}
    for f in findings:
        if ledger.mode == "doc":
            workers, problems, snippets = _judge_doc_inputs(ledger, rnd, f, results, snap, cfg)
            verify[f.id] = {"writer": snippets}
            doc_problems[f.id] = problems
            writer_patches[f.id] = workers["writer"].patch
            if s.needs_checker(workers["a"], workers["b"], problems, workers["writer"].patch):
                pending.append(f)
                if checker_results is not None and f"{f.id}:checker" in checker_results:
                    workers["checker"] = checker_results[f"{f.id}:checker"]
        else:
            d, workers, verify[f.id] = _judge_code(ledger, rnd, f, results, snap, cfg, baseline, repro)
            decisions.append(d)
        for role, w in workers.items():
            merged[f"{f.id}:{role}"] = w.to_dict()
            if role in ("a", "b", "writer"):
                s.patch_path(rd, f.id, role).write_text(w.patch, encoding="utf-8")
    s.write_json(rd / "results.json", merged)
    s.write_json(rd / "verify.json", verify)

    if ledger.mode == "doc":
        if pending and checker_results is None:
            out = rd / "checker-args.json"
            wts = {f.id: {"writer": s.worktree(snap, f.id, "writer")} for f in pending}
            jobs = build_checker_jobs(s.prompt_findings(ledger, pending), wts, writer_patches, ledger.workers or None)
            claude_args, cli_args = split_job_args(jobs, rd / "prompts")
            s.write_json(out, claude_args)
            if cli_args["jobs"]:
                s.write_json(rd / "checker-cli-args.json", cli_args)
            else:
                (rd / "checker-cli-args.json").unlink(missing_ok=True)
            s.mark(rnd, "judge-checker")
            s.save_ledger(run_dir, ledger)
            print(f"점검자 작업 {len(claude_args['jobs'])}건: {out}")
            if cli_args["jobs"]:
                print(f"CLI 점검자 작업 {len(cli_args['jobs'])}건: {rd / 'checker-cli-args.json'}")
            _print_next(ledger, run_dir)
            return 0
        for f in findings:
            a, b = s.worker_from(merged, f.id, "a"), s.worker_from(merged, f.id, "b")
            checker = s.worker_from(merged, f.id, "checker") if f"{f.id}:checker" in merged else None
            d = decide_doc(a, b, checker, doc_problems[f.id])
            decisions.append(s.without_empty_patch(d, {"writer": writer_patches[f.id]}, adjudicated=False))

    by_id = {f.id: f for f in s.prompt_findings(ledger, findings)}
    adj_items = []
    for d in decisions:
        if d.outcome == OUTCOME_ADJUDICATE:
            name = s.wt_name(d.finding_id, "adjudicator")
            wt = snap.worktrees[name] if name in snap.worktrees else s.new_worktree(snap, name, s.base_of(ledger, rnd), cfg)
            adj_items.append(s.adj_input(ledger, by_id[d.finding_id], merged, verify, wt, cfg, d.reasons))
    if adj_items:
        s.write_json(rd / "adj-args.json", write_job_args(build_adjudicator_jobs(adj_items), rd / "prompts"))
    held = {f["id"] for f in rnd.hold_findings}
    rnd.decisions = [d for d in rnd.decisions if d["finding_id"] in held] + [d.to_dict() for d in decisions]
    s.write_json(rd / "decisions.json", rnd.decisions)
    ledger.snapshot = snap.to_dict()
    rnd.approved_repro = dict(repro or {})
    s.mark(rnd, "judge", repro_allowed=repro is not None)
    s.save_ledger(run_dir, ledger)
    print("판정: " + ", ".join(f"{k} {v}" for k, v in Counter(d.outcome for d in decisions).items()))
    _print_next(ledger, run_dir)
    return 0


def _adjudicate_verify(ledger, rnd, f, wt, patch, snap, cfg, rd):
    if ledger.mode == "doc":
        missing, snippets = s.doc_checks(wt, snap.worktrees.get(s.base_name(rnd)), patch, ledger, cfg)
        results = s.doc_verify(missing, snippets)
        return results, results
    judged, recorded = s.worker_verify(wt, cfg, changed_files(patch), s.baseline_failed(rd))
    cmds = rnd.approved_repro.get(f.id)
    if ledger.source == s.SOURCE_PR and patch.strip() and cmds:
        extra = run_repro(snap.worktrees[s.base_name(rnd)], wt, cmds)
        judged += extra
        recorded += extra
    return judged, recorded


def cmd_adjudicate(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "adjudicate", args.force)
    if not s.step_done(rnd, "judge"):
        raise s.CliError(f"회차 {rnd.no}의 judge 단계를 먼저 한다")
    s.check_user_tree(ledger, rnd)
    cfg = s.cfg_for(ledger.source, Path(ledger.root))
    snap = s.snapshot_of(ledger)
    rd = s.round_dir(run_dir, rnd.no)
    results = read_results(s.read_json(args.results))
    for warning in s.model_warnings(results):
        print(warning)
    merged = s.read_json(rd / "results.json")
    verify = s.read_json(rd / "verify.json")
    by_id = {f.id: f for f in s.prompt_findings(ledger, s.findings_of(rnd))}
    new_decisions = []
    opinion_jobs = []
    for data in rnd.decisions:
        d = Decision.from_dict(data)
        if d.outcome != OUTCOME_ADJUDICATE:
            new_decisions.append(data)
            continue
        f = by_id[d.finding_id]
        wt = s.worktree(snap, f.id, "adjudicator")
        adj = s.pick(results, f.id, "adjudicator")
        adj.patch = extract(wt, s.base_of(ledger, rnd), cfg)
        vadj, vrecorded = _adjudicate_verify(ledger, rnd, f, wt, adj.patch, snap, cfg, rd)
        if ledger.mode == "code" and s.is_sensitive(changed_files(adj.patch), cfg) and REASON_SENSITIVE not in d.reasons:
            d.reasons.append(REASON_SENSITIVE)
        patches = {role: s.worker_from(merged, f.id, role).patch for role in ("a", "b", "writer")}
        new = s.without_empty_patch(after_adjudication(d, adj, vadj), {**patches, "adjudicator": adj.patch}, adjudicated=True)
        if adj.patch.strip():
            s.patch_path(rd, f.id, "adjudicator").write_text(adj.patch, encoding="utf-8")
        merged[f"{f.id}:adjudicator"] = adj.to_dict()
        verify.setdefault(f.id, {})["adjudicator"] = [r.to_dict() for r in vrecorded]
        if new.outcome == OUTCOME_UNDECIDABLE:
            item = s.adj_input(ledger, f, merged, verify, wt, cfg, new.reasons)
            prompt = build_opinion_prompt(item, adj)
            (rd / f"opinion-{f.id}.md").write_text(prompt, encoding="utf-8")
            opinion_jobs.append((s.opinion_job_id(f.id), f"{f.id} opinion", prompt,
                                 snap.worktrees.get(s.base_name(rnd)) or ledger.root))
        new_decisions.append(new.to_dict())
    rnd.decisions = new_decisions
    if opinion_jobs and s.is_claude(ledger):
        s.write_review_args(rd, s.OPINION_ARGS, ledger, opinion_jobs)
    s.write_json(rd / "results.json", merged)
    s.write_json(rd / "verify.json", verify)
    s.write_json(rd / "decisions.json", rnd.decisions)
    s.mark(rnd, "adjudicate")
    s.save_ledger(run_dir, ledger)
    print("판가름 뒤: " + ", ".join(f"{k} {v}" for k, v in Counter(d["outcome"] for d in new_decisions).items()))
    _print_next(ledger, run_dir)
    return 0


def _claude_opinions(args, run_dir: Path, ledger: Ledger, rnd: RoundRecord) -> int:
    rd = s.round_dir(run_dir, rnd.no)
    pending = s.pending_opinions(ledger, rd) if not args.force else [
        p.stem[len("opinion-"):] for p in sorted(rd.glob("opinion-*.md"))]
    if not pending:
        raise s.CliError(f"회차 {rnd.no}에 붙일 의견이 없다. 다시 붙이려면 --force")
    results = read_review_results(s.read_json(Path(args.results)))
    for fid in pending:
        text, model_id = results.get(s.opinion_job_id(fid), ("", None))
        warning = model_warning(s.opinion_job_id(fid), model_id, ledger.review_model)
        if warning:
            print(warning)
        if not text.strip():
            print(f"경고: {fid} Claude 의견이 없다(호출 실패). 「{CALL_FAILED_NOTE}」로 적고 넘어간다")
            text = CALL_FAILED_NOTE
        ledger.opinions[fid] = text.strip()
        s.mark(rnd, f"opinion:{fid}")
    s.save_ledger(run_dir, ledger)
    print(f"Claude 의견 {len(pending)}건을 원장에 붙였다(자동 채택하지 않는다)")
    _print_next(ledger, run_dir)
    return 0


EXTERNAL_AUTH_FAILURE = re.compile(r"\b401\b|unauthori[sz]ed|refresh[ _]token|codex login|not logged in|Authentication required", re.IGNORECASE)
EXTERNAL_USAGE_LIMIT = re.compile(r"usage limit", re.IGNORECASE)
EXTERNAL_LOG_TAIL_LINES = 5
LOGIN_COMMANDS = {REVIEWER_CODEX: "codex login", REVIEWER_CURSOR: "cursor-agent login"}


def _external_result_text(result: Path, what: str, reviewer: str) -> str:
    """외부 검토자(Codex·Cursor) 결과(`<로그>.final`)를 읽는다. 없거나 비면 실행 로그와 인증 실패·이용 한도 여부를 짚어 멈춘다."""
    text = result.read_text(encoding="utf-8").strip() if result.is_file() else ""
    if text:
        return text
    problem = "없다" if not result.is_file() else "비어 있다"
    log = result.with_suffix("") if result.suffix == ".final" else None
    lines = [f"{label(reviewer)} {what} 결과 파일이 {problem}: {result}"]
    if log is None or not log.is_file():
        lines.append(f"실행 로그도 없다({log or '로그 경로를 알 수 없다'}). RR 명령의 --out 경로와 종료 코드를 확인한다")
    else:
        log_text = log.read_text(encoding="utf-8", errors="replace")
        tail = log_text.strip().splitlines()[-EXTERNAL_LOG_TAIL_LINES:]
        lines.append(f"실행 로그: {log}")
        lines.extend(f"  | {line}" for line in tail)
        if EXTERNAL_AUTH_FAILURE.search(log_text):
            lines.append("로그에 인증 실패 흔적이 있다(401·refresh token·Authentication required).")
        if EXTERNAL_USAGE_LIMIT.search(log_text):
            lines.append("로그에 이용 한도 도달 흔적이 있다(usage limit). 한도가 풀린 뒤 RR을 다시 돌린다.")
    lines.append(f"인증 실패면 터미널에서 `{LOGIN_COMMANDS.get(reviewer, LOGIN_COMMANDS[REVIEWER_CODEX])}`으로 다시 로그인한 뒤 RR을 다시 돌린다.")
    if reviewer == REVIEWER_CODEX:
        lines.append("RR은 한 번에 하나씩 돌린다(동시에 돌리면 refresh token이 겹쳐 소모돼 로그인이 풀릴 수 있다)")
    raise s.CliError("\n".join(lines))


def cmd_opinion(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    if args.results:
        return _claude_opinions(args, run_dir, ledger, rnd)
    if not args.id or not args.result:
        raise s.CliError("--id 와 --result 를 함께 주거나(Codex), --results 하나만 준다(Claude Workflow 결과)")
    s.guard(rnd, f"opinion:{args.id}", args.force)
    if args.id not in {d["finding_id"] for d in rnd.decisions}:
        raise s.CliError(f"회차 {rnd.no}에 {args.id} 판정이 없다")
    text = _external_result_text(Path(args.result), "의견", ledger.reviewer)
    ledger.opinions[args.id] = text
    s.mark(rnd, f"opinion:{args.id}")
    s.save_ledger(run_dir, ledger)
    print(f"{args.id} {label(ledger.reviewer)} 의견을 원장에 붙였다(자동 채택하지 않는다)")
    _print_next(ledger, run_dir)
    return 0


def _final_suite(root: Path, base_wt: Path | None, cfg, mode: str, changed: list[str]) -> dict:
    cfg, warning = s.final_suite_cfg(cfg, mode, root, changed)
    if warning:
        print(warning)
    if cfg is None or not cfg.full_tests:
        return {"final_ok": None, "final_tail": ""}
    results = run_verifiers(root, cfg, changed, "final")
    tail = "\n".join(r.tail for r in results if not r.ok)[-FINAL_TAIL_CHARS:]
    final = {"final_ok": all(r.ok for r in results), "final_tail": tail}
    if not final["final_ok"] and base_wt is not None and Path(base_wt).is_dir():
        final["baseline_failed"] = not all(r.ok for r in run_verifiers(base_wt, cfg, changed, "final"))
    return final


def _adopted_patches(rnd: RoundRecord, rd: Path) -> list[tuple[str, str]]:
    patches = []
    for d in rnd.decisions:
        if s.is_adopted(d):
            patches.append((d["finding_id"], s.read_text(s.patch_path(rd, d["finding_id"], d["patch_from"]))))
    return patches


def _record_apply(run_dir: Path, ledger: Ledger, rnd: RoundRecord, rd: Path, report: ApplyReport, data: dict,
                  patches: list[tuple[str, str]]) -> None:
    carried = s.carried_ids(rnd, report.applied, [fid for fid, _ in report.failed], dict(patches))
    for fid, tail in report.failed:
        reason = f"{REASON_APPLY_FAILED}: {s.first_line(tail)}"
        if fid in carried:
            _set_decision(rnd, fid, OUTCOME_CARRIED, f"{reason} — {REASON_CARRIED}")
        else:
            _set_decision(rnd, fid, OUTCOME_UNDECIDABLE, reason)
    data["carried"] = [fid for fid, _ in report.failed if fid in carried]
    rnd.adopted = list(report.applied)
    s.write_json(rd / "apply.json", data)
    s.write_json(rd / "decisions.json", rnd.decisions)
    s.mark(rnd, "apply-patched", result_commit=rnd.result_commit)
    s.save_ledger(run_dir, ledger)


def _patch_dry_run(run_dir, ledger, rnd, rd, patches) -> Path:
    snap = s.snapshot_of(ledger)
    name = f"result-r{rnd.no}"
    if name in snap.worktrees:
        remove_worktree(snap, name)
    wt = add_worktree(snap, name, s.base_of(ledger, rnd))
    ledger.snapshot = snap.to_dict()
    report = apply_all(wt, patches)
    rnd.result_commit = commit_worktree(wt, f"cascade-fix {ledger.run_id} 회차 {rnd.no} 결과(dry-run)") or s.base_of(ledger, rnd)
    data = {"dry_run": True, **report.to_dict(), "would_apply": list(report.applied), "result_worktree": str(wt)}
    _record_apply(run_dir, ledger, rnd, rd, report, data, patches)
    print(f"dry-run: 작업 트리에는 적용하지 않고 {wt} 에 적용했다 (채택 {len(report.applied)}건, 실패 {len(report.failed)}건)")
    return wt


def _patch_user_tree(run_dir, ledger, rnd, rd, patches, root) -> None:
    report = apply_all(root, patches, s.external_docs(ledger))
    rnd.result_commit = snapshot_tree(s.snapshot_of(ledger), list(s.external_docs(ledger).values()))
    _record_apply(run_dir, ledger, rnd, rd, report, report.to_dict(), patches)
    print(f"적용 {len(report.applied)}건, 실패 {len(report.failed)}건")


def _remove_round_worktrees(ledger: Ledger, rnd: RoundRecord) -> None:
    snap = s.snapshot_of(ledger)
    for name in s.round_worktree_names(rnd):
        if name in snap.worktrees:
            remove_worktree(snap, name)
    ledger.snapshot = snap.to_dict()


def cmd_apply(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "apply", args.force)
    if not s.step_done(rnd, "judge"):
        raise s.CliError(f"회차 {rnd.no}의 judge 단계를 먼저 한다")
    if any(d["outcome"] == OUTCOME_ADJUDICATE for d in rnd.decisions) and not s.step_done(rnd, "adjudicate"):
        raise s.CliError(f"회차 {rnd.no}의 판가름(adjudicate)을 먼저 한다")
    root = Path(ledger.root)
    cfg = s.cfg_for(ledger.source, root)
    rd = s.round_dir(run_dir, rnd.no)
    recheck_repo = str(root)
    if s.step_done(rnd, "apply-patched"):
        print("패치는 이미 적용했다(apply-patched) — 적용을 건너뛰고 뒷단계만 한다")
        data = s.read_json(rd / "apply.json")
        if ledger.dry_run:
            recheck_repo = data.get("result_worktree", recheck_repo)
    else:
        patches = _adopted_patches(rnd, rd)
        if ledger.dry_run:
            recheck_repo = str(_patch_dry_run(run_dir, ledger, rnd, rd, patches))
        else:
            _patch_user_tree(run_dir, ledger, rnd, rd, patches, root)
        data = s.read_json(rd / "apply.json")
    texts = {fid: s.read_text(s.patch_path(rd, fid, d["patch_from"]))
             for d in rnd.decisions if s.is_adopted(d) for fid in [d["finding_id"]]}
    applied = [fid for fid in data.get("applied", []) if fid in texts]
    changed = list(dict.fromkeys(p for fid in applied for p in changed_files(texts[fid])))
    if not ledger.dry_run:
        _remove_round_worktrees(ledger, rnd)
        base_wt = s.snapshot_of(ledger).worktrees.get(s.base_name(rnd))
        final = _final_suite(root, base_wt, cfg, ledger.mode, changed) if applied else {"final_ok": None, "final_tail": ""}
        data.update(final)
        if final["final_ok"] is False:
            data["interference"] = True
            reason = REASON_FINAL_BASELINE_FAILED if final.get("baseline_failed") else REASON_INTERFERENCE
            for fid in [*applied, *data.get("carried", [])]:
                _set_decision(rnd, fid, OUTCOME_UNDECIDABLE, reason)
            data["carried"] = []
            rnd.adopted = []
            print(f"{reason}. 적용한 패치는 작업 트리에 그대로 두었다 — 되돌릴지는 사람이 정한다")
    s.write_json(rd / "apply.json", data)
    s.write_json(rd / "decisions.json", rnd.decisions)
    if s.recheck_needed(ledger, rnd):
        adopted = [f for f in s.findings_of(rnd) if f.id in rnd.adopted]
        scope = list(dict.fromkeys(p for fid in rnd.adopted if fid in texts for p in changed_files(texts[fid])))
        if rnd.result_commit:
            diff = s.range_diff(root, s.base_of(ledger, rnd), rnd.result_commit, scope)
        else:
            diff = "".join(texts[f.id] for f in adopted)
        prompt = build_recheck_prompt(adopted, diff, ledger.mode)
        (rd / "recheck-prompt.md").write_text(prompt, encoding="utf-8")
        (rd / s.RECHECK_REPO_FILE).write_text(recheck_repo + "\n", encoding="utf-8")
        if s.is_claude(ledger):
            s.write_review_args(rd, s.RECHECK_ARGS, ledger, [(s.recheck_job_id(rnd.no), f"회차 {rnd.no} recheck", prompt, recheck_repo)])
        s.write_json(rd / s.RECHECK_SCOPE_FILE, scope)
        rnd.recheck_opened = True
        print(f"재검토 필요: {rd / 'recheck-prompt.md'} (--repo {recheck_repo})")
    elif s.carried_findings(rnd):
        nxt = _open_round(run_dir, ledger, rnd, s.renumber(s.carried_findings(rnd), rnd.no + 1, ledger.source))
        print(f"재검토 없음: 적용하지 못한 {len(nxt.findings)}건으로 회차 {nxt.no}을 연다")
    else:
        ledger.status = STATUS_DONE
        print("재검토 없음: 종료")
    s.mark(rnd, "apply")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def _open_round(run_dir: Path, ledger: Ledger, rnd: RoundRecord, findings: list[Finding]) -> RoundRecord:
    no = rnd.no + 1
    write_findings(s.round_dir(run_dir, no) / "findings.json", findings)
    nxt = RoundRecord(no=no, findings=[f.to_dict() for f in findings], decisions=[], adopted=[], recheck_opened=False,
                      started_at=s.now(), base_commit=rnd.result_commit or s.base_of(ledger, rnd))
    ledger.rounds.append(nxt)
    return nxt


def _hold_reason(f, rnd: RoundRecord, max_reached: bool, outside: set[str]) -> str:
    if f.id in outside:
        return REASON_OUT_OF_SCOPE
    if rnd.final:
        return "「지금 반영」 뒤 마지막 재검토: 회차를 열지 않음"
    if f.severity == "Suggestion":
        return "재검토의 Suggestion: 회차를 열지 않음"
    return "재검토 상한 도달" if max_reached else "수렴: Critical 없고 2건 이하"


def cmd_recheck(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, "recheck", args.force)
    if not rnd.recheck_opened:
        raise s.CliError(f"회차 {rnd.no}은 재검토를 열지 않았다")
    is_json = args.result.endswith(".json")
    if not is_json:
        _external_result_text(Path(args.result), "재검토", ledger.reviewer)
    elif not Path(args.result).is_file():
        raise s.CliError(f"파일이 없다: {args.result}")
    text, model_id = load_text(Path(args.result), s.recheck_job_id(rnd.no) if is_json else None)
    if not text.strip():
        raise s.CliError(f"재검토 결과가 비어 있다(Workflow 호출 실패일 수 있다). 재검토 Workflow를 다시 돌린다: {args.result}")
    warning = model_warning(s.recheck_job_id(rnd.no), model_id, ledger.review_model)
    if warning:
        print(warning)
    rd = s.round_dir(run_dir, rnd.no)
    (rd / "recheck-result.md").write_text(text, encoding="utf-8")
    try:
        resolved, new = parse_recheck(text, rnd.no + 1)
    except ParseError as e:
        raise s.CliError(f"재검토 결과 {e}", ExitCodes.PARSE_ERROR) from None
    if not any(line.strip() == RESOLUTION_HEADER for line in text.splitlines()):
        print(f"경고: 「{RESOLUTION_HEADER}」 절이 없다. 고친 지적을 모두 해소 안 됨으로 본다")
        resolved = {}
    adopted = [f for f in s.findings_of(rnd) if f.id in rnd.adopted]
    missing = [f.id for f in adopted if f.id not in resolved]
    if resolved and missing:
        print(f"경고: 해소 여부가 없는 지적은 해소 안 됨으로 본다: {', '.join(missing)}")
    unresolved = [s.unresolved_copy(f) for f in adopted if resolved.get(f.id) is not True]
    numbered = s.renumber(unresolved + new + s.carried_findings(rnd), rnd.no + 1, ledger.source)
    combined, carried = numbered[:len(unresolved) + len(new)], numbered[len(unresolved) + len(new):]
    max_reached = s.rechecks_done(ledger) >= ledger.max_rechecks
    in_scope = s.recheck_scope(ledger, rd, adopted, combined)
    outside = {f.id for f in out_of_scope(combined, in_scope)}
    next_round, hold, undecidable = classify_recheck(combined, rnd, max_reached, final=rnd.final, in_scope=in_scope)
    for f in hold:
        rnd.hold_findings.append(f.to_dict())
        rnd.decisions.append(Decision(f.id, OUTCOME_HOLD, None, [_hold_reason(f, rnd, max_reached, outside)]).to_dict())
    for f in undecidable:
        reason = "「지금 반영」 뒤 재검토의 Critical" if rnd.final else "같은 위치 Critical 두 회차 연속"
        rnd.hold_findings.append(f.to_dict())
        rnd.decisions.append(Decision(f.id, OUTCOME_UNDECIDABLE, None, [reason]).to_dict())
    s.write_json(rd / "decisions.json", rnd.decisions)
    s.mark(rnd, "recheck")
    print(f"해소 {sum(1 for v in resolved.values() if v)}건, 해소 안 됨 {len(unresolved)}건, 새 지적 {len(new)}건")
    next_round += carried
    print(f"다음 회차 {len(next_round)}건(적용 못 해 넘긴 {len(carried)}건 포함), "
          f"보류 후보 {len(hold)}건(범위 밖 {len(outside)}건), 판단 불가 {len(undecidable)}건")
    if next_round:
        _open_round(run_dir, ledger, rnd, next_round)
    else:
        ledger.status = STATUS_DONE
    print(f"status={ledger.status}")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def cmd_resume(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    if ledger.status != STATUS_DONE and not args.force:
        raise s.CliError("아직 진행 중이다. 회차가 끝난 뒤(status=종료) 보류 후보를 고른다")
    choices = s.read_json(args.choices)
    if not isinstance(choices, dict) or not choices:
        raise s.CliError("선택 파일은 {\"<지적 id>\": \"지금 반영|구현 때 테스트로|보류\"} 형식이다")
    if any(s.step_done(r, "cleanup") for r in ledger.rounds):
        raise s.CliError("cleanup으로 스냅샷을 이미 지웠다. resume은 cleanup 전에 한다")
    holds = {d["finding_id"]: r for r in ledger.rounds for d in r.decisions if d["outcome"] == OUTCOME_HOLD}
    for fid, choice in choices.items():
        if fid not in holds:
            raise s.CliError(f"보류 후보가 아니다: {fid}")
        if choice not in HOLD_CHOICES:
            raise s.CliError(f"알 수 없는 선택: {choice} ({' / '.join(HOLD_CHOICES)})")
        if fid in ledger.hold_choices and not args.force:
            raise s.CliError(f"{fid}는 이미 골랐다({ledger.hold_choices[fid]}). 바꾸려면 --force")
    ledger.hold_choices.update(choices)
    chosen = [fid for fid, c in choices.items() if c == CHOICE_NOW]
    if chosen:
        no = max(r.no for r in ledger.rounds) + 1
        findings = [next(f for f in [*holds[fid].findings, *holds[fid].hold_findings] if f["id"] == fid) for fid in chosen]
        last = ledger.rounds[-1]
        rnd = RoundRecord(no=no, findings=findings, decisions=[], adopted=[], recheck_opened=False,
                          final=True, started_at=s.now(), base_commit=last.result_commit or last.base_commit)
        ledger.rounds.append(rnd)
        s.write_json(s.round_dir(run_dir, no) / "findings.json", findings)
        ledger.status = STATUS_RUNNING
        print(f"「지금 반영」 {len(chosen)}건으로 회차 {no}을 연다(마지막 회차: 재검토 한 번, 그 지적은 목록으로만)")
    else:
        print("「지금 반영」 없음: 새 회차를 열지 않는다")
    s.mark(ledger.current_round(), "resume")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


def _interference_note(ledger: Ledger, run_dir: Path) -> str:
    lines = []
    for rnd in ledger.rounds:
        path = run_dir / f"round-{rnd.no}" / "apply.json"
        data = s.read_json(path) if path.is_file() else {}
        if data.get("interference"):
            lines += [
                f"- 회차 {rnd.no}: 적용한 패치 {', '.join(data.get('applied', []))} 를 모두 넣은 뒤 전체 테스트가 깨졌다.",
                "  작업 트리는 되돌리지 않았다. 되돌릴지·어느 패치를 뺄지는 사람이 정한다.",
                *(["  기준선(수정 전)에서도 전체 테스트가 실패했다."] if data.get("baseline_failed") else []),
                f"  마지막 출력: {s.first_line(data.get('final_tail', ''))}",
            ]
    return "\n".join(["", "## 간섭 의심", "", *lines, ""]) if lines else ""


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    text = render(ledger, ledger.dry_run, run_dir) + _interference_note(ledger, run_dir)
    write_prepared(run_dir, ledger)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    rnd = ledger.current_round()
    if rnd is not None:
        s.mark(rnd, "report", round=rnd.no)
        s.save_ledger(run_dir, ledger)
    print(text)
    print(f"보고서: {run_dir / 'report.md'} · 준비됨: {run_dir / 'prepared'}")
    return 0


def cmd_cleanup(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = ledger.current_round()
    s.guard(rnd, "cleanup", args.force)
    snap = s.snapshot_of(ledger)
    cleanup(snap, keep_run_dir=True)
    ledger.snapshot = snap.to_dict()
    s.mark(rnd, "cleanup")
    s.save_ledger(run_dir, ledger)
    print(f"임시 브랜치 {snap.branch}와 워크트리를 지웠다. 원장은 남긴다: {run_dir}")
    return 0


def cmd_status(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = ledger.current_round()
    print(f"run_id={ledger.run_id} source={ledger.source} mode={ledger.mode} status={ledger.status}")
    if rnd is not None:
        steps = [e.get("step") for e in rnd.events]
        print(f"회차 {rnd.no}{' (마지막)' if rnd.final else ''}: 지적 {len(rnd.findings)}건, 한 단계 {', '.join(steps) or '없음'}")
    _print_next(ledger, run_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cascade.cli", description="리뷰 지적 캐스케이드 처리")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, *, run_dir=True, round_no=False, force=True):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        if run_dir:
            p.add_argument("--run-dir", required=True)
        if round_no:
            p.add_argument("--round", type=int, required=True)
        if force:
            p.add_argument("--force", action="store_true")
        return p

    def add_workers_args(p):
        p.add_argument("--workers", choices=WORKERS_CHOICES,
                       help="1단계 작업자(기본: 환경 변수 CASCADE_WORKERS, 없으면 claude). "
                            "external: 쓸 수 있는 Codex·Cursor CLI에 A·writer, B·checker 자리를 배정")
        p.add_argument("--cursor-model", help=f"Cursor 작업자 모델(기본 {DEFAULT_CURSOR_MODEL})")

    p = add("init", cmd_init, run_dir=False, force=False)
    p.add_argument("--source", choices=SOURCES, required=True)
    p.add_argument("--input")
    p.add_argument("--findings-json")
    p.add_argument("--doc", action="append")
    p.add_argument("--pr-repo")
    p.add_argument("--me")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--adopt-on-both-pass", action="store_true",
                   help="코드 모드에서 같은 판정에 패치가 달라도 둘 다 검증을 통과하면 판가름 없이 작은 패치를 채택")
    p.add_argument("--max-rechecks", type=int, default=2)
    p.add_argument("--root")
    p.add_argument("--consent", help="전송에 동의한 외부 수신처(codex,cursor 중 쉼표 구분, 또는 none). 기본: 이 프로필이 쓰는 수신처 전부")
    p.add_argument("--reviewer", choices=REVIEWER_CHOICES,
                   help="판가름 뒤 의견·재검토를 맡을 검토자(기본: 환경 변수 CASCADE_REVIEWER, 없으면 auto). "
                        "auto: claude-* 입력이면 claude, 아니면 codex-review 스킬이 있으면 codex, 없으면 cursor를 쓸 수 있을 때 cursor, 아니면 claude")
    p.add_argument("--review-model", help=f"Claude 검토자 모델(기본 {DEFAULT_MODEL})")
    p.add_argument("--review-effort", choices=EFFORTS, help=f"Claude 검토자 effort(기본 {DEFAULT_EFFORT})")
    p.add_argument("--profile", help="역할 프로필 이름(기본: 환경 변수 CASCADE_PROFILE, repos.toml, 없으면 내장 default)")
    p.add_argument("--severity", help="이번 실행에서 처리할 등급(쉼표 구분, 예: Critical,Warning). 나머지는 보류 후보")
    add_workers_args(p)
    p = add("detect", cmd_detect, run_dir=False, force=False)
    p.add_argument("--input")
    p.add_argument("--doc", action="append", help="검토할 문서(설계·계획 문서). 변경으로 잡히지 않는 커밋한 문서도 지정한다")
    p.add_argument("--pr")
    p.add_argument("--root")
    p.add_argument("--no-gh", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--reviewer", choices=REVIEWER_CHOICES)
    p.add_argument("--profile", help="역할 프로필 이름(기본: 환경 변수 CASCADE_PROFILE, repos.toml, 없으면 내장 default)")
    add_workers_args(p)
    p = add("review", cmd_review, run_dir=False, force=False)
    p.add_argument("--kind", choices=("code", "doc"), required=True)
    p.add_argument("--base")
    p.add_argument("--uncommitted", action="store_true")
    p.add_argument("--commit")
    p.add_argument("--doc", action="append")
    p.add_argument("--focus")
    p.add_argument("--root")
    p.add_argument("--profile", help="역할 프로필 이름(기본: 환경 변수 CASCADE_PROFILE, repos.toml, 없으면 내장 default)")
    p.add_argument("--review-model")
    p.add_argument("--review-effort", choices=EFFORTS)
    add("jobs", cmd_jobs, round_no=True)
    p = add("workers", cmd_workers, round_no=True)
    p.add_argument("--stage", choices=(s.STAGE1, s.STAGE_CHECKER), required=True)
    p = add("judge", cmd_judge, round_no=True)
    p.add_argument("--results")
    p.add_argument("--checker-results")
    p.add_argument("--allow-repro")
    p = add("adjudicate", cmd_adjudicate, round_no=True)
    p.add_argument("--results", required=True)
    p = add("opinion", cmd_opinion, round_no=True)
    p.add_argument("--id")
    p.add_argument("--result")
    p.add_argument("--results", help="Claude 검토자 Workflow 결과(opinion.json). --id/--result 대신 쓴다")
    add("apply", cmd_apply, round_no=True)
    p = add("recheck", cmd_recheck, round_no=True)
    p.add_argument("--result", required=True)
    p = add("resume", cmd_resume)
    p.add_argument("--choices", required=True)
    add("report", cmd_report, force=False)
    add("cleanup", cmd_cleanup)
    add("status", cmd_status, force=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except s.CliError as e:
        print(f"오류: {e.message}")
        return e.code
    except RuntimeError as e:
        print(f"오류: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
