import argparse
import os
import re
import shlex
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from cascade import cli_steps as s
from cascade.config import ExitCodes
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
    REASON_SENSITIVE,
    VERDICT_FACT,
    Decision,
    after_adjudication,
    decide,
    decide_doc,
)
from cascade.findings import (
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
    write_job_args,
)
from cascade.patches import ApplyReport, apply_all, changed_files, extract
from cascade.report import render, write_prepared
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

SOURCES = ("codex-doc", "codex-code", "pr-comment")
HOLD_CHOICES = ("지금 반영", "구현 때 테스트로", "보류")
CHOICE_NOW = "지금 반영"
REASON_APPLY_FAILED = "패치 적용 실패"
REASON_INTERFERENCE = "전체 테스트 실패(간섭 의심)"
REASON_FINAL_BASELINE_FAILED = "전체 테스트 실패(기준선도 실패)"
REASON_OUT_OF_SCOPE = "재검토 범위 밖"
RAW_PREVIEW_LINES = 5
FINAL_TAIL_CHARS = 2000


def _print_next(ledger: Ledger, run_dir: Path) -> None:
    print("다음:")
    for line in s.next_steps(ledger, run_dir):
        print(f"  {line}")


def _set_decision(rnd: RoundRecord, fid: str, outcome: str, reason: str) -> None:
    for d in rnd.decisions:
        if d["finding_id"] == fid:
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
    raw = s.read_text(Path(args.input))
    parser = parse_codex_doc if args.source == "codex-doc" else parse_codex_code
    try:
        return parser(raw, 1), raw
    except ParseError as e:
        raise s.CliError(str(e), ExitCodes.PARSE_ERROR) from None


def cmd_init(args) -> int:
    root = Path(args.root).resolve() if args.root else s.git_toplevel(Path.cwd())
    s.cfg_for(args.source, root)
    findings, raw = _parse_input(args, root)
    if not findings:
        print("지적 0건 — 원문을 확인하라")
        for line in raw.splitlines()[:RAW_PREVIEW_LINES]:
            print(f"  | {line}")
        if "[P" in raw:
            print("경고: 원문에 「[P」가 있다. 파서가 항목을 놓쳤을 수 있다")
        print(f"status={STATUS_DONE}")
        return 0
    docs, external = _split_docs(root, args.doc or [])
    ensure_excluded(root)
    snap = create_snapshot(root, _new_run_id(root), external)
    stamp = s.now()
    ledger = Ledger(
        run_id=snap.run_id,
        source=args.source,
        mode="doc" if args.source == "codex-doc" else "code",
        root=str(root),
        snapshot=snap.to_dict(),
        dry_run=args.dry_run,
        max_rechecks=args.max_rechecks,
        rounds=[RoundRecord(no=1, findings=[f.to_dict() for f in findings], decisions=[], adopted=[],
                            recheck_opened=False, started_at=stamp, events=[{"step": "init", "at": stamp}],
                            base_commit=snap.commit)],
        docs=docs,
    )
    write_findings(s.round_dir(snap.run_dir, 1) / "findings.json", findings)
    s.save_ledger(snap.run_dir, ledger)
    counts = Counter(f.severity for f in findings)
    print(f"run_dir={snap.run_dir}")
    print(f"status={STATUS_RUNNING}")
    print(f"지적 {len(findings)}건: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if args.dry_run:
        print("dry-run: 채택 패치를 작업 트리에 적용하지 않는다")
    _print_next(ledger, snap.run_dir)
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
    if ledger.mode == "code" and cfg.tests:
        located = list(dict.fromkeys(p for f in findings if (p := location_file(f.location))))
        failed = s.write_baseline(rd, base_wt, cfg, located)
        if failed:
            print(f"기준선에서 이미 실패하는 검증 명령(판정에서 제외): {', '.join(failed)}")
    if ledger.source == s.SOURCE_PR:
        listed = {f.id: repro_cmds(f) for f in findings}
        s.write_json(rd / s.REPRO_FILE, listed)
        if any(listed.values()):
            print(f"재현 명령(승인 전에는 돌리지 않는다): {rd / s.REPRO_FILE}")
            for fid, cmds in listed.items():
                for cmd in cmds:
                    print(f"  {fid}: {cmd}")
    out = rd / "stage1-args.json"
    s.write_json(out, write_job_args(build_stage1_jobs(findings, cfg, wts, ledger.mode), rd / "prompts"))
    ledger.snapshot = snap.to_dict()
    fingerprint = user_tree_fingerprint(snap.root, snap.run_dir)
    s.mark(rnd, "jobs", worktrees={fid: {r: str(p) for r, p in m.items()} for fid, m in wts.items()},
           fingerprint=fingerprint)
    s.save_ledger(run_dir, ledger)
    print(f"작업 {len(findings) * len(roles)}건: {out}")
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
    d = s.without_empty_patch(decide(a, b, va, vb, pa, pb, sensitive), {"a": pa, "b": pb}, adjudicated=False)
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
    results = read_results(s.read_json(args.results))
    checker_results = read_results(s.read_json(args.checker_results)) if args.checker_results else None
    baseline = s.baseline_failed(rd)
    repro = s.approved_repro(rd, args.allow_repro) if ledger.source == s.SOURCE_PR else None
    for warning in s.model_warnings({**results, **(checker_results or {})}):
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
            jobs = build_checker_jobs(s.prompt_findings(ledger, pending), wts, writer_patches)
            s.write_json(out, write_job_args(jobs, rd / "prompts"))
            s.mark(rnd, "judge-checker")
            s.save_ledger(run_dir, ledger)
            print(f"점검자 작업 {len(pending)}건: {out}")
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
    rnd.decisions = [d.to_dict() for d in decisions]
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
            (rd / f"opinion-{f.id}.md").write_text(build_opinion_prompt(item, adj), encoding="utf-8")
        new_decisions.append(new.to_dict())
    rnd.decisions = new_decisions
    s.write_json(rd / "results.json", merged)
    s.write_json(rd / "verify.json", verify)
    s.write_json(rd / "decisions.json", rnd.decisions)
    s.mark(rnd, "adjudicate")
    s.save_ledger(run_dir, ledger)
    print("판가름 뒤: " + ", ".join(f"{k} {v}" for k, v in Counter(d["outcome"] for d in new_decisions).items()))
    _print_next(ledger, run_dir)
    return 0


def cmd_opinion(args) -> int:
    run_dir = Path(args.run_dir)
    ledger = s.load_ledger(run_dir)
    rnd = s.get_round(ledger, args.round)
    s.guard(rnd, f"opinion:{args.id}", args.force)
    if args.id not in {d["finding_id"] for d in rnd.decisions}:
        raise s.CliError(f"회차 {rnd.no}에 {args.id} 판정이 없다")
    text = s.read_text(Path(args.result)).strip()
    if not text:
        raise s.CliError(f"의견 파일이 비어 있다: {args.result}")
    ledger.opinions[args.id] = text
    s.mark(rnd, f"opinion:{args.id}")
    s.save_ledger(run_dir, ledger)
    print(f"{args.id} Codex 의견을 원장에 붙였다(자동 채택하지 않는다)")
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


def _record_apply(run_dir: Path, ledger: Ledger, rnd: RoundRecord, rd: Path, report: ApplyReport, data: dict) -> None:
    for fid, tail in report.failed:
        _set_decision(rnd, fid, OUTCOME_UNDECIDABLE, f"{REASON_APPLY_FAILED}: {s.first_line(tail)}")
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
    _record_apply(run_dir, ledger, rnd, rd, report, data)
    print(f"dry-run: 작업 트리에는 적용하지 않고 {wt} 에 적용했다 (채택 {len(report.applied)}건, 실패 {len(report.failed)}건)")
    return wt


def _patch_user_tree(run_dir, ledger, rnd, rd, patches, root) -> None:
    report = apply_all(root, patches, s.external_docs(ledger))
    rnd.result_commit = snapshot_tree(s.snapshot_of(ledger), list(s.external_docs(ledger).values()))
    _record_apply(run_dir, ledger, rnd, rd, report, report.to_dict())
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
            for fid in applied:
                _set_decision(rnd, fid, OUTCOME_UNDECIDABLE, reason)
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
        (rd / "recheck-prompt.md").write_text(build_recheck_prompt(adopted, diff, ledger.mode), encoding="utf-8")
        (rd / s.RECHECK_REPO_FILE).write_text(recheck_repo + "\n", encoding="utf-8")
        s.write_json(rd / s.RECHECK_SCOPE_FILE, scope)
        rnd.recheck_opened = True
        print(f"재검토 필요: {rd / 'recheck-prompt.md'} (--repo {recheck_repo})")
    else:
        ledger.status = STATUS_DONE
        print("재검토 없음: 종료")
    s.mark(rnd, "apply")
    s.save_ledger(run_dir, ledger)
    _print_next(ledger, run_dir)
    return 0


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
    text = s.read_text(Path(args.result))
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
    combined = s.renumber(unresolved + new, rnd.no + 1, ledger.source)
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
    print(f"다음 회차 {len(next_round)}건, 보류 후보 {len(hold)}건(범위 밖 {len(outside)}건), 판단 불가 {len(undecidable)}건")
    if next_round:
        no = rnd.no + 1
        write_findings(s.round_dir(run_dir, no) / "findings.json", next_round)
        ledger.rounds.append(RoundRecord(no=no, findings=[f.to_dict() for f in next_round], decisions=[],
                                         adopted=[], recheck_opened=False, started_at=s.now(),
                                         base_commit=rnd.result_commit or s.base_of(ledger, rnd)))
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

    p = add("init", cmd_init, run_dir=False, force=False)
    p.add_argument("--source", choices=SOURCES, required=True)
    p.add_argument("--input")
    p.add_argument("--findings-json")
    p.add_argument("--doc", action="append")
    p.add_argument("--pr-repo")
    p.add_argument("--me")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-rechecks", type=int, default=2)
    p.add_argument("--root")
    add("jobs", cmd_jobs, round_no=True)
    p = add("judge", cmd_judge, round_no=True)
    p.add_argument("--results", required=True)
    p.add_argument("--checker-results")
    p.add_argument("--allow-repro")
    p = add("adjudicate", cmd_adjudicate, round_no=True)
    p.add_argument("--results", required=True)
    p = add("opinion", cmd_opinion, round_no=True)
    p.add_argument("--id", required=True)
    p.add_argument("--result", required=True)
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
