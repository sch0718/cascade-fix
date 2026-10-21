import json
from datetime import datetime
from pathlib import Path

from cascade import assignment
from cascade.cli_steps import exhausted_runners, review_cell
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_AGREED_NO,
    OUTCOME_DESIGN,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
    REASON_BOTH_PASS,
)
from cascade.profiles import ROLE_KEYS
from cascade.review import CLAUDE_SOURCES
from cascade.review import label as reviewer_label
from cascade.rounds import Ledger
from cascade.runners import STATUS_AUTH, STATUS_OK
from cascade.workers import FALLBACK_LABELS, LOGIN_COMMANDS, WORKERS_CLAUDE, describe, fallback_counts

SOURCE_PR_COMMENT = "pr-comment"
BUCKET_REBUTTAL = "반박"
BUCKET_DESIGN = "설계 결정"
BUCKET_UNDECIDABLE = "판단 불가"
BUCKET_HOLD = "보류 후보"
BUCKETS = (BUCKET_REBUTTAL, BUCKET_DESIGN, BUCKET_UNDECIDABLE, BUCKET_HOLD)
ADJUDICATED_NO = "판가름: 아님"
ADJUDICATION_MARK = "판가름"
JOB_FILES = ("stage1-args.json", "stage1-cli-args.json", "checker-args.json", "checker-cli-args.json", "adj-args.json")
CLI_TOKEN_KEYS = (("input", "input_tokens"), ("output", "output_tokens"))
OPINION_MAX_LINES = 20
COMMIT_SUBJECT_MAX = 60
REBUTTAL_CHOICE = "선택: [ ] 동의 [ ] 그래도 고친다"
HOLD_CHOICE = "선택: [ ] 지금 반영 [ ] 구현 때 테스트로 [ ] 보류"
DESIGN_CHOICE = "선택: [ ] 직접 정함 [ ] 보류"
DESIGN_CHOICE_WITH_OPTIONS = "선택: [ ] 선택지 ___ [ ] 직접 정함 [ ] 보류"
DESIGN_CHOICE_WITH_PATCH = "선택: [ ] 작성자 안 채택(패치 직접 적용) [ ] 직접 정함 [ ] 보류"
DRY_RUN_NOT_APPLIED = "적용 안 됨(dry-run)"
REPLY_ADOPTED = "반영했습니다."
REPLY_REBUTTAL = "확인 결과 고치지 않았습니다."
REPLY_PENDING = "검토 중입니다."
UNTIMED_STEPS = ("report",)
TABLE_FOOTNOTE = (
    "합의-아님은 합의 칸에 한 번 세고 사람이 볼 목록의 반박 칸에도 오른다. "
    "판가름 회부는 판가름에 들어간 지적 전부이고, 판가름 채택은 그 가운데 패치를 채택한 지적이다. "
    "합의·판가름 채택은 판정 때 값이라 적용 실패나 전체 테스트 실패로 판단 불가가 된 지적도 그대로 센다. "
    "사람 칸은 설계결정·판단불가·보류후보와 판가름 뒤에도 채택하지 못한 지적이다(적용 단계에서 판단 불가가 된 지적 포함). "
    "앞 패치와 겹쳐 적용하지 못하고 다음 회차로 넘긴 지적은 사람 칸에 세지 않고 다음 회차 지적에 다시 센다."
)


def _first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _is_adopted(decision: dict) -> bool:
    outcome = decision.get("outcome")
    return outcome == OUTCOME_AGREED_FACT or (outcome == OUTCOME_ADJUDICATE and decision.get("patch_from") is not None)


def _is_adjudicated_rebuttal(decision: dict) -> bool:
    return (
        decision.get("outcome") == OUTCOME_ADJUDICATE
        and decision.get("patch_from") is None
        and any(ADJUDICATED_NO in reason for reason in decision.get("reasons", []))
    )


def _was_adjudicated(decision: dict) -> bool:
    return decision.get("outcome") == OUTCOME_ADJUDICATE or any(
        reason.startswith(ADJUDICATION_MARK) for reason in decision.get("reasons", [])
    )


def _bucket_of(decision: dict) -> str | None:
    outcome = decision.get("outcome")
    if outcome == OUTCOME_AGREED_NO or _is_adjudicated_rebuttal(decision):
        return BUCKET_REBUTTAL
    if outcome == OUTCOME_DESIGN:
        return BUCKET_DESIGN
    if outcome == OUTCOME_UNDECIDABLE or (outcome == OUTCOME_ADJUDICATE and decision.get("patch_from") is None):
        return BUCKET_UNDECIDABLE
    if outcome == OUTCOME_HOLD:
        return BUCKET_HOLD
    return None


def _finding_of(rnd, finding_id: str) -> dict:
    for f in [*rnd.findings, *rnd.hold_findings]:
        if f.get("id") == finding_id:
            return f
    return {"id": finding_id, "severity": "-", "location": "-", "body": "", "thread_id": None}


def _bucketed(ledger: Ledger) -> dict[str, list[tuple[int, dict, dict]]]:
    buckets: dict[str, list[tuple[int, dict, dict]]] = {name: [] for name in BUCKETS}
    for rnd in ledger.rounds:
        for d in rnd.decisions:
            name = _bucket_of(d)
            if name is not None:
                buckets[name].append((rnd.no, _finding_of(rnd, d["finding_id"]), d))
    return buckets


def bucket_decisions(ledger: Ledger) -> dict[str, list[tuple[dict, dict]]]:
    return {name: [(f, d) for _, f, d in items] for name, items in _bucketed(ledger).items()}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_results(run_dir: Path | None, round_no: int) -> dict | None:
    if run_dir is None:
        return None
    data = _read_json(Path(run_dir) / f"round-{round_no}" / "results.json")
    return data if isinstance(data, dict) else None


def _load_apply(run_dir: Path | None, round_no: int) -> dict | None:
    if run_dir is None:
        return None
    data = _read_json(Path(run_dir) / f"round-{round_no}" / "apply.json")
    return data if isinstance(data, dict) else None


def _failed_items(apply: dict | None) -> list[tuple[str, str]]:
    carried = set((apply or {}).get("carried", []) or [])
    items = []
    for entry in (apply or {}).get("failed", []) or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2 and str(entry[0]) not in carried:
            items.append((str(entry[0]), str(entry[1])))
    return items


def _failed_ids(ledger: Ledger, run_dir: Path | None) -> dict[tuple[int, str], str]:
    failed: dict[tuple[int, str], str] = {}
    for rnd in ledger.rounds:
        for fid, tail in _failed_items(_load_apply(run_dir, rnd.no)):
            failed[(rnd.no, fid)] = tail
    return failed


def _worker_reason(results: dict | None, finding_id: str, role: str) -> str | None:
    if not results:
        return None
    item = results.get(f"{finding_id}:{role}")
    if not isinstance(item, dict):
        return None
    return str(item.get("reason", "")).strip()


def _fence(text: str) -> str:
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _fenced(text: str) -> str:
    fence = _fence(text)
    return f"{fence}\n{text}\n{fence}"


def _item_line(finding: dict, decision: dict) -> str:
    carried = f" (회차를 넘어옴: {finding['carried_from']})" if finding.get("carried_from") else ""
    return (
        f"- **{finding.get('id', decision['finding_id'])}** "
        f"({finding.get('severity', '-')}, {finding.get('location', '-')}) — {_first_line(finding.get('body', ''))}{carried}"
    )


def _judge_lines(decision: dict, results: dict | None) -> list[str]:
    fid = decision["finding_id"]
    a = _worker_reason(results, fid, "a")
    b = _worker_reason(results, fid, "b")
    if a and b:
        extra = [r for r in decision.get("reasons", []) if not r.startswith(("a:", "b:"))]
        return [f"a: {a}", f"b: {b}", *extra]
    return list(decision.get("reasons", []))


def _design_lines(ledger: Ledger, results: dict | None, round_no: int, fid: str) -> list[str]:
    """설계 결정 칸의 작성자 결과. 작성자가 설계 결정으로 봤으면 선택지를, 고쳤으면 채택하지 않은 수정안과 패치 경로를 붙인다."""
    item = (results or {}).get(f"{fid}:writer") if ledger.mode == "doc" else None
    if not isinstance(item, dict):
        return [f"  - {DESIGN_CHOICE}"]
    reason = [line for line in str(item.get("reason", "")).splitlines() if line.strip()]
    if item.get("needs_design_decision"):
        return ["  - 작성자 선택지:", *(f"    {line}" for line in reason), f"  - {DESIGN_CHOICE_WITH_OPTIONS}"]
    if str(item.get("patch", "")).strip():
        return ["  - 작성자 수정안(채택 안 함):", *(f"    {line}" for line in reason),
                f"  - 패치: round-{round_no}/patch-{fid}-writer.diff", f"  - {DESIGN_CHOICE_WITH_PATCH}"]
    return [*([f"  - 작성자: {' '.join(reason)}"] if reason else []), f"  - {DESIGN_CHOICE}"]


def _has_adjudication_note(reasons: list[str], opus: str) -> bool:
    prefix = f"{ADJUDICATION_MARK}:"
    return any(r.startswith(prefix) and r[len(prefix):].strip() == opus for r in reasons)


def _review_label(ledger: Ledger, role: str) -> str:
    return reviewer_label(review_cell(ledger, role)["runner"])


def _recorded_event(rnd, step: str) -> dict | None:
    found = [e for e in rnd.events if e.get("step") == step and e.get("runner")]
    return found[-1] if found else None


def _opinion_event(ledger: Ledger, fid: str) -> dict | None:
    for rnd in reversed(ledger.rounds):
        event = _recorded_event(rnd, f"opinion:{fid}")
        if event:
            return event
    return None


def _event_runner(event: dict | None, fallback: str) -> str:
    return event["runner"] if event else fallback


def _opinion_runner(ledger: Ledger, fid: str) -> str:
    return _event_runner(_opinion_event(ledger, fid), review_cell(ledger, "opinion")["runner"])


def _recheck_event(ledger: Ledger, rnd) -> dict | None:
    return _recorded_event(rnd, "recheck")


def _recheck_runner(ledger: Ledger, rnd) -> str:
    return _event_runner(_recheck_event(ledger, rnd), review_cell(ledger, "recheck")["runner"])


def _opinion_labels(ledger: Ledger) -> str:
    runners = list(dict.fromkeys(_opinion_runner(ledger, fid) for fid in ledger.opinions))
    return "·".join(reviewer_label(r) for r in runners) or _review_label(ledger, "opinion")


def _indent_reasons(lines: list[str]) -> list[str]:
    return [f"  - {line}" for line in lines]


def _bucket_section(name: str, items: list[tuple[int, dict, dict]], ledger: Ledger, run_dir: Path | None) -> list[str]:
    out = [f"### {name}", ""]
    if not items:
        return [*out, "- 없음", ""]
    for round_no, finding, decision in items:
        fid = decision["finding_id"]
        results = _load_results(run_dir, round_no)
        out.append(_item_line(finding, decision))
        if name == BUCKET_REBUTTAL:
            label = "판가름" if decision["outcome"] == OUTCOME_ADJUDICATE else "합의"
            out.append(f"  - 경로: {label}")
            out.extend(_indent_reasons(_judge_lines(decision, results)))
            out.append(f"  - {REBUTTAL_CHOICE}")
        elif name == BUCKET_DESIGN:
            out.extend(_indent_reasons(_judge_lines(decision, results)))
            out.extend(_design_lines(ledger, results, round_no, fid))
        elif name == BUCKET_UNDECIDABLE:
            reasons = decision.get("reasons", [])
            out.extend(_indent_reasons(reasons))
            opus = _worker_reason(results, fid, "adjudicator")
            if opus and not _has_adjudication_note(reasons, opus):
                out.append(f"  - Opus: {opus}")
            opinion = ledger.opinions.get(fid)
            if opinion:
                out.append(f"  - {reviewer_label(_opinion_runner(ledger, fid))} 의견:")
                out.extend(f"    > {line}" for line in opinion.splitlines()[:OPINION_MAX_LINES])
            if ledger.mode == "doc":
                out.append(f"  - 패치 작성자: round-{round_no}/patch-{fid}-writer.diff")
            else:
                out.append(f"  - 패치 a: round-{round_no}/patch-{fid}-a.diff")
                out.append(f"  - 패치 b: round-{round_no}/patch-{fid}-b.diff")
            if run_dir is not None and (run_dir / f"round-{round_no}" / f"patch-{fid}-adj.diff").is_file():
                out.append(f"  - 패치 판가름: round-{round_no}/patch-{fid}-adj.diff")
        else:
            out.extend(_indent_reasons(decision.get("reasons", [])))
            out.append(f"  - {HOLD_CHOICE}")
            chosen = ledger.hold_choices.get(fid)
            if chosen:
                out.append(f"  - 선택됨: {chosen}")
    out.append("")
    return out


def _round_row(rnd) -> str:
    consensus = sum(1 for d in rnd.decisions
                    if d["outcome"] in (OUTCOME_AGREED_FACT, OUTCOME_AGREED_NO) or d.get("adopted_as") == OUTCOME_AGREED_FACT)
    sent = sum(1 for d in rnd.decisions if _was_adjudicated(d))
    adjudicated = sum(1 for d in rnd.decisions
                      if (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from") is not None)
                      or d.get("adopted_as") == OUTCOME_ADJUDICATE)
    steps = {e.get("step") for e in rnd.events}
    codex = sum(1 for f in rnd.findings if f"opinion:{f.get('id')}" in steps)
    human = sum(
        1
        for d in rnd.decisions
        if d["outcome"] in (OUTCOME_DESIGN, OUTCOME_UNDECIDABLE, OUTCOME_HOLD)
        or (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from") is None)
    )
    return f"| {rnd.no} | {len(rnd.findings)} | {consensus} | {sent} | {adjudicated} | {codex} | {human} |"


def _adopt_on_both_pass_note(ledger: Ledger) -> list[str]:
    if not ledger.adopt_on_both_pass:
        return []
    n = sum(1 for rnd in ledger.rounds for d in rnd.decisions
            if OUTCOME_AGREED_FACT in (d["outcome"], d.get("adopted_as"))
            and any(r.startswith(REASON_BOTH_PASS) for r in d.get("reasons", [])))
    return [f"이번 실행은 --adopt-on-both-pass 로 돌았다. 같은 판정에 패치가 달라도 둘 다 검증을 통과하면 "
            f"판가름 없이 작은 패치를 채택해 합의 칸에 센다. 이 규칙으로 채택한 지적 {n}건.", ""]


def _adopted_pairs(ledger: Ledger, failed: dict[tuple[int, str], str]) -> list[tuple[dict, dict]]:
    pairs = []
    for rnd in ledger.rounds:
        for d in rnd.decisions:
            if _is_adopted(d) and (rnd.no, d["finding_id"]) not in failed:
                pairs.append((_finding_of(rnd, d["finding_id"]), d))
    return pairs


def _bullet(finding: dict) -> str:
    head = f"- {finding['id']} {finding.get('location', '-')}"
    first = _first_line(finding.get("body", ""))
    return f"{head}: {first}" if first else head


def _commit_message(ledger: Ledger, failed: dict[tuple[int, str], str]) -> str:
    pairs = _adopted_pairs(ledger, failed)
    if not pairs:
        return ""
    if len(pairs) == 1:
        first = _first_line(pairs[0][0].get("body", ""))[:COMMIT_SUBJECT_MAX]
        subject = f"fix: {first}" if first else f"fix: 리뷰 지적 {pairs[0][0]['id']} 반영"
    else:
        subject = f"fix: {len(pairs)}건의 리뷰 지적 반영"
    bullets = [_bullet(f) for f, _ in pairs]
    return "\n".join([subject, "", *bullets]) + "\n"


def _final_decisions(ledger: Ledger) -> dict[str, tuple[int, dict]]:
    final: dict[str, tuple[int, dict]] = {}
    for rnd in ledger.rounds:
        for d in rnd.decisions:
            final[d["finding_id"]] = (rnd.no, d)
    return final


def _thread_findings(ledger: Ledger) -> dict[str, list[dict]]:
    threads: dict[str, list[dict]] = {}
    for rnd in ledger.rounds:
        for f in rnd.findings:
            thread_id = f.get("thread_id")
            if not thread_id:
                continue
            items = threads.setdefault(thread_id, [])
            if all(item["id"] != f["id"] for item in items):
                items.append(f)
    return threads


def _rebuttal_grounds(decision: dict, results: dict | None) -> list[str]:
    fid = decision["finding_id"]
    reasons = decision.get("reasons", [])
    if decision["outcome"] == OUTCOME_ADJUDICATE:
        opus = _worker_reason(results, fid, "adjudicator")
        prefix = f"{ADJUDICATION_MARK}:"
        notes = [r[len(prefix):].strip() for r in reasons if r.startswith(prefix) and r != ADJUDICATED_NO]
        grounds = [opus] if opus else notes[-1:]
    else:
        a = _worker_reason(results, fid, "a")
        b = _worker_reason(results, fid, "b")
        grounds = [a, b] if a and b else [r.split(":", 1)[1].strip() for r in reasons if r.startswith(("a:", "b:"))]
    return [g for g in dict.fromkeys(grounds) if g]


def _finding_reply(fid: str, entry: tuple[int, dict] | None, run_dir: Path | None,
                   failed: dict[tuple[int, str], str], summary: str) -> str:
    if entry is None:
        return REPLY_PENDING
    round_no, d = entry
    if _is_adopted(d) and (round_no, fid) in failed:
        return f"적용 실패: {_first_line(failed[(round_no, fid)])}. {REPLY_PENDING}"
    if _is_adopted(d):
        return " ".join([REPLY_ADOPTED, summary]).strip()
    if _bucket_of(d) == BUCKET_REBUTTAL:
        grounds = _rebuttal_grounds(d, _load_results(run_dir, round_no))
        return " ".join([REPLY_REBUTTAL, " / ".join(grounds)]).strip()
    return REPLY_PENDING


def _reply_drafts(ledger: Ledger, run_dir: Path | None, failed: dict[tuple[int, str], str]) -> dict[str, str]:
    final = _final_decisions(ledger)
    drafts: dict[str, str] = {}
    for thread_id, findings in _thread_findings(ledger).items():
        if len(findings) == 1:
            f = findings[0]
            drafts[thread_id] = _finding_reply(f["id"], final.get(f["id"]), run_dir, failed, _first_line(f.get("body", "")))
            continue
        paragraphs = []
        for f in findings:
            summary = _first_line(f.get("body", ""))
            reply = _finding_reply(f["id"], final.get(f["id"]), run_dir, failed, "")
            paragraphs.append(f"> {summary}\n{reply}" if summary else reply)
        drafts[thread_id] = "\n\n".join(paragraphs)
    return drafts


def _resolve_targets(ledger: Ledger, failed: dict[tuple[int, str], str]) -> list[str]:
    final = _final_decisions(ledger)

    def settled(fid: str) -> bool:
        entry = final.get(fid)
        return entry is not None and _is_adopted(entry[1]) and (entry[0], fid) not in failed

    return [thread_id for thread_id, findings in _thread_findings(ledger).items()
            if all(settled(f["id"]) for f in findings)]


def _prepared_section(ledger: Ledger, run_dir: Path | None) -> list[str]:
    out = ["## 준비됨", "", "### 커밋 메시지 초안", ""]
    failed = _failed_ids(ledger, run_dir)
    message = _commit_message(ledger, failed)
    out.extend([_fenced(message.rstrip("\n")) if message else "(채택된 지적 없음)", ""])
    if ledger.source == SOURCE_PR_COMMENT and ledger.dry_run:
        out.extend(["### PR 답글 초안", "", DRY_RUN_NOT_APPLIED, "", "### resolve 대상", "", DRY_RUN_NOT_APPLIED, ""])
    elif ledger.source == SOURCE_PR_COMMENT:
        out.extend(["### PR 답글 초안", ""])
        drafts = _reply_drafts(ledger, run_dir, failed)
        if not drafts:
            out.append("- 없음")
        for thread_id, draft in drafts.items():
            out.extend([f"thread {thread_id}:", draft, ""])
        out.extend(["### resolve 대상", ""])
        targets = _resolve_targets(ledger, failed)
        out.extend([f"- {t}" for t in targets] or ["- 없음"])
        out.append("")
    apply_lines = []
    for rnd in ledger.rounds:
        applied = _load_apply(run_dir, rnd.no)
        if applied is None:
            continue
        apply_lines.append(f"- 회차 {rnd.no} 적용: {', '.join(applied.get('applied', [])) or '없음'}")
        if applied.get("carried"):
            apply_lines.append(f"- 회차 {rnd.no} 다음 회차로 넘김(앞 패치와 겹침): {', '.join(applied['carried'])}")
        for fid, tail in _failed_items(applied):
            apply_lines.extend([f"- 회차 {rnd.no} 실패 **{fid}**:", "", _fenced(str(tail)), ""])
    if apply_lines:
        out.extend(["### 적용 결과", "", *apply_lines, ""])
    return out


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _elapsed(rnd) -> str:
    times = [t for t in (_parse_time(e.get("at", "")) for e in rnd.events if e.get("step") not in UNTIMED_STEPS)
             if t is not None]
    if len(times) < 2:
        return "-"
    try:
        seconds = int((times[-1] - times[0]).total_seconds())
    except TypeError:
        return "-"
    minutes, rest = divmod(max(seconds, 0), 60)
    return f"{minutes}분 {rest}초" if minutes else f"{rest}초"


def _reason_text(reasons: dict[str, int]) -> str:
    total = sum(reasons.values())
    if not total:
        return "0"
    named = {reason: n for reason, n in reasons.items() if reason}
    if len(named) == 1 and sum(named.values()) == total:
        return f"{total}({next(iter(named))})"
    return f"{total}(" + ", ".join(f"{reason} {n}" if reason else f"사유 미상 {n}" for reason, n in reasons.items()) + ")"


def _add_reason(table: dict[str, dict[str, int]], key: str, reason: str) -> None:
    counts = table.setdefault(key, {})
    counts[reason] = counts.get(reason, 0) + 1


def _job_ids(run_dir: Path, round_no: int) -> set[str] | None:
    rd = Path(run_dir) / f"round-{round_no}"
    ids: set[str] = set()
    found = False
    for name in JOB_FILES:
        data = _read_json(rd / name)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            found = True
            ids.update(j["id"] for j in data["jobs"] if isinstance(j, dict) and isinstance(j.get("id"), str))
    results = _load_results(run_dir, round_no)
    if results is not None:
        found = True
        ids.update(results)
    return ids if found else None


def _cell_calls(ledger: Ledger, run_dir: Path | None) -> dict[str, int] | None:
    if run_dir is None:
        return None
    counts: dict[str, int] = {}
    found = False
    for rnd in ledger.rounds:
        ids = _job_ids(run_dir, rnd.no)
        if ids is None:
            continue
        found = True
        for job_id in ids:
            key = assignment.job_cell_key(ledger.mode, job_id.rsplit(":", 1)[-1])
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts if found else None


def _stage_cli_data(run_dir: Path, no: int, stage: str) -> dict | None:
    data = _read_json(Path(run_dir) / f"round-{no}" / f"{stage}-cli.json")
    return data if isinstance(data, dict) else None


def _last_stage_events(ledger: Ledger) -> dict[tuple[int, str], dict]:
    return {(no, stage): event for no, stage, event in _worker_events(ledger)}


def _cell_of(ledger: Ledger, job_id: str) -> str | None:
    return assignment.job_cell_key(ledger.mode, job_id.rsplit(":", 1)[-1])


def _worker_fallbacks(ledger: Ledger, run_dir: Path | None) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """칸별 대체 실행 사유 건수와 칸별 CLI 토큰. 처음 실행의 실패 상태를 사유로 삼고, 외부로 다시 돌려 성공해도 센다."""
    reasons: dict[str, dict[str, int]] = {}
    tokens: dict[str, dict[str, int]] = {}
    for (no, stage), event in _last_stage_events(ledger).items():
        data = _stage_cli_data(run_dir, no, stage) if run_dir is not None else None
        outcomes = data.get("outcomes") if data else None
        if not isinstance(outcomes, list):
            for job_id in event.get("fallback") or []:
                if key := _cell_of(ledger, job_id):
                    _add_reason(reasons, key, "")
            continue
        first = {o["id"]: o for o in data.get("replaced") or [] if isinstance(o, dict) and "id" in o}
        for outcome in outcomes:
            if not isinstance(outcome, dict) or "id" not in outcome:
                continue
            key = _cell_of(ledger, outcome["id"])
            if key is None:
                continue
            status = first.get(outcome["id"], outcome).get("status")
            if status != STATUS_OK:
                _add_reason(reasons, key, FALLBACK_LABELS.get(status, str(status)))
            total = tokens.setdefault(key, {"input": 0, "output": 0})
            for run in (outcome, first.get(outcome["id"])):
                usage = (run or {}).get("usage") or {}
                for name, usage_key in CLI_TOKEN_KEYS:
                    total[name] += usage.get(usage_key, 0)
    return reasons, tokens


def _token_text(tokens: dict[str, int] | None) -> str:
    return f"입력 {tokens['input']} · 출력 {tokens['output']}" if tokens else "-"


def _first_review_runner(ledger: Ledger) -> str | None:
    actual = (assignment.ledger_profile(ledger).get("first_review") or {}).get("actual") or {}
    if actual.get("runner"):
        return actual["runner"]
    return "claude" if ledger.source in CLAUDE_SOURCES else None


def _first_review_attempts(ledger: Ledger) -> list[dict]:
    return (assignment.ledger_profile(ledger).get("first_review") or {}).get("attempts") or []


def _review_rows(runners: list[str], events: list[dict | None], fallback: str, what: str) -> list[str]:
    counts: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    for runner, event in zip(runners, events):
        counts[runner] = counts.get(runner, 0) + 1
        if event and event.get("fallback"):
            _add_reason(reasons, runner, str(event["fallback"].get("reason", "")))
    rows = [f"| {reviewer_label(r)} {what} | {n} | {_reason_text(reasons.get(r, {}))} | - |" for r, n in counts.items()]
    return rows or [f"| {reviewer_label(fallback)} {what} | 0 | 0 | - |"]


def _cli_usage(ledger: Ledger) -> dict[str, dict[str, int]]:
    usage: dict[str, dict[str, int]] = {}
    for _, _, event in _worker_events(ledger):
        for runner, tokens in (event.get("usage") or {}).items():
            sums = usage.setdefault(runner, {"input": 0, "output": 0, "cache_read": 0})
            for key in sums:
                sums[key] += tokens.get(key, 0)
    return usage


def _calls_section(ledger: Ledger, run_dir: Path | None) -> list[str]:
    profile = assignment.ledger_profile(ledger)
    calls = _cell_calls(ledger, run_dir)
    reasons, tokens = _worker_fallbacks(ledger, run_dir)
    out = ["## 역할별 사용량", "", "| 역할 | 호출 | 대체 실행(사유) | CLI 토큰 |", "| --- | ---: | ---: | ---: |"]
    for key in assignment.work_keys(profile["roles"], ledger.mode):
        fallback = _reason_text(reasons[key]) if key in reasons else "0" if calls is not None else "-"
        out.append(f"| {key} | {calls.get(key, 0) if calls is not None else '-'} | {fallback} | {_token_text(tokens.get(key))} |")
    first = _first_review_runner(ledger)
    if first:
        attempts = _first_review_attempts(ledger)
        fallback = {attempts[-1].get("reason", "")} if attempts else set()
        out.append(f"| {reviewer_label(first)} 첫 검토 | 1 | {_reason_text({r: 1 for r in fallback})} | - |")
    opinions = list(ledger.opinions)
    out.extend(_review_rows([_opinion_runner(ledger, fid) for fid in opinions], [_opinion_event(ledger, fid) for fid in opinions],
                            review_cell(ledger, "opinion")["runner"], "의견"))
    rechecks = [r for r in ledger.rounds if r.recheck_opened]
    out.extend(_review_rows([_recheck_runner(ledger, r) for r in rechecks], [_recheck_event(ledger, r) for r in rechecks],
                            review_cell(ledger, "recheck")["runner"], "재검토"))
    usage = _cli_usage(ledger)
    if usage:
        out.extend(["", "| CLI | 입력 토큰 | 출력 토큰 | 캐시 읽기 토큰 |", "| --- | ---: | ---: | ---: |"])
        out.extend(f"| {runner} | {t['input']} | {t['output']} | {t['cache_read']} |" for runner, t in usage.items())
    out.extend(["", "| 회차 | 소요 |", "| --- | ---: |"])
    out.extend(f"| {rnd.no} | {_elapsed(rnd)} |" for rnd in ledger.rounds)
    out.append("")
    return out


def _worker_events(ledger: Ledger) -> list[tuple[int, str, dict]]:
    return [(rnd.no, e["step"][len("workers-"):], e) for rnd in ledger.rounds for e in rnd.events
            if str(e.get("step", "")).startswith("workers-")]


def _auth_runners(ledger: Ledger, run_dir: Path | None) -> list[str]:
    found = [runner for rnd in ledger.rounds for runner, entry in exhausted_runners(rnd).items()
             if entry.get("status") == STATUS_AUTH and runner in LOGIN_COMMANDS]
    if run_dir is not None:
        for no, stage, _ in _worker_events(ledger):
            data = _read_json(Path(run_dir) / f"round-{no}" / f"{stage}-cli.json")
            outcomes = data.get("outcomes", []) if isinstance(data, dict) else []
            found += [o["runner"] for o in outcomes if o.get("status") == STATUS_AUTH and o.get("runner") in LOGIN_COMMANDS]
    return list(dict.fromkeys(found))


def _fallback_text(cell: dict) -> str:
    fallback = cell.get("fallback") or {}
    if not fallback.get("runner"):
        return "-"
    detail = " ".join(part for part in (fallback.get("model"), fallback.get("effort")) if part)
    return f"{fallback['runner']}({detail})" if detail else fallback["runner"]


def _cell_row(key: str, cell: dict) -> str:
    return f"| {key} | {cell['runner']} | {cell['model'] or '-'} | {cell['effort'] or '-'} | {_fallback_text(cell)} |"


def _substitution_line(record: dict, to: str) -> str:
    return f"{record['role']} {record['from']} → {to} — {record['reason']}"


def _first_review_line(first_review: dict) -> str:
    suffix = "" if first_review.get("attempts") else " — 대체 없음"
    return f"첫 검토 {assignment.first_review_chain(first_review)}{suffix}"


def _runtime_lines(ledger: Ledger, profile: dict) -> list[str]:
    lines = [f"- {_substitution_line(r, r['to'])} (회차 {r.get('round', '-')})"
             for r in profile.get("runtime_substitutions", []) if r["role"].startswith(f"{ledger.mode}.")]
    for no, stage, event in _worker_events(ledger):
        total = sum((event.get("counts") or {}).values())
        n, reasons = fallback_counts(event.get("counts") or {})
        lines.append(f"- 회차 {no} {stage}: CLI 작업 {total}건, 대체 실행 {n}건" + (f"({reasons})" if n else ""))
        if event.get("fallback_runs"):
            lines.append(f"- 회차 {no} {stage} 외부 CLI로 다시 돌림: "
                         + ", ".join(f"{job_id} → {runner}" for job_id, runner in event["fallback_runs"].items()))
    return lines


def _roles_section(ledger: Ledger, run_dir: Path | None) -> list[str]:
    profile = assignment.ledger_profile(ledger)
    mode = ledger.mode
    out = ["## 역할 배정", "", f"프로필: {profile['name']} ({profile['source']})"]
    if profile.get("overrides"):
        out.append(assignment.overrides_line(profile["overrides"]))
    if ledger.workers_choice != WORKERS_CLAUDE and ledger.workers:
        out.append(f"배정: {describe(ledger.workers)}")
    out.extend(["", "| 역할 | runner | 모델 | effort | fallback |", "| --- | --- | --- | --- | --- |"])
    out.extend(_cell_row(key, profile["roles"][key]) for key in ROLE_KEYS if key.startswith(f"{mode}.") and key in profile["roles"])
    out.append("")
    changed = [f"- {_substitution_line(r, r['to'])}" for r in profile.get("substituted", []) if r["role"].startswith(f"{mode}.")]
    if changed:
        out.extend(["### 배정 때 바꾼 칸(init·jobs)", "", *changed, ""])
    if profile.get("first_review"):
        out.extend(["### 첫 검토 이력", "", f"- {_first_review_line(profile['first_review'])}", ""])
    runtime = _runtime_lines(ledger, profile)
    logins = [f"{runner}: 로그인이 풀렸다. `{LOGIN_COMMANDS[runner]}` 로 다시 로그인한 뒤 이어서 돌린다."
              for runner in _auth_runners(ledger, run_dir)]
    if runtime or logins:
        out.extend(["### 실행 중 대체", "", *runtime, *([""] if runtime and logins else []), *logins, ""])
    return out


def render(ledger: Ledger, dry_run: bool, run_dir: Path | None = None) -> str:
    run_dir = Path(run_dir) if run_dir is not None else None
    out = [f"# cascade-fix 보고서 {ledger.run_id}", ""]
    if dry_run:
        out.extend(["**적용하지 않음(dry-run)**", ""])
    out.extend([f"출처: {ledger.source} · 모드: {ledger.mode} · 검토자: {_opinion_labels(ledger)} · 상태: {ledger.status}", ""])
    out.extend(
        [
            "## 회차별 지적과 채택 경로",
            "",
            f"| 회차 | 지적 | 합의 | 판가름 회부 | 판가름 채택 | {_opinion_labels(ledger)} 검토 | 사람 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            *[_round_row(rnd) for rnd in ledger.rounds],
            "",
            TABLE_FOOTNOTE,
            "",
            *_adopt_on_both_pass_note(ledger),
            "## 사람이 볼 목록",
            "",
        ]
    )
    for name, items in _bucketed(ledger).items():
        out.extend(_bucket_section(name, items, ledger, run_dir))
    out.extend(_baseline_section(ledger, run_dir))
    out.extend(_prepared_section(ledger, run_dir))
    out.extend(_roles_section(ledger, run_dir))
    out.extend(_calls_section(ledger, run_dir))
    return "\n".join(out).rstrip("\n") + "\n"


def _baseline_section(ledger: Ledger, run_dir: Path | None) -> list[str]:
    if run_dir is None:
        return []
    lines = []
    for rnd in ledger.rounds:
        data = _read_json(Path(run_dir) / f"round-{rnd.no}" / "baseline.json")
        failed = data.get("failed", []) if isinstance(data, dict) else []
        if failed:
            lines.append(f"- 회차 {rnd.no}: 수정 전부터 실패한 검증 명령(판정에서 제외): " + ", ".join(f"`{c}`" for c in failed))
        if (_load_apply(run_dir, rnd.no) or {}).get("baseline_failed"):
            lines.append(f"- 회차 {rnd.no}: 수정 뒤 전체 테스트가 실패했고 기준선(수정 전)에서도 실패했다. 적용분은 판단 불가로 보냈다")
    return ["## 기준선 실패", "", *lines, ""] if lines else []


def write_prepared(run_dir: Path, ledger: Ledger) -> None:
    prepared = Path(run_dir) / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    failed = _failed_ids(ledger, Path(run_dir))
    (prepared / "commit-message.txt").write_text(_commit_message(ledger, failed), encoding="utf-8")
    pr = ledger.source == SOURCE_PR_COMMENT and not ledger.dry_run
    replies = _reply_drafts(ledger, Path(run_dir), failed) if pr else {}
    (prepared / "replies.json").write_text(json.dumps(replies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (prepared / "resolve.json").write_text(
        json.dumps(_resolve_targets(ledger, failed) if pr else [], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
