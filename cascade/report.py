import json
from datetime import datetime
from pathlib import Path

from cascade.cli_steps import exhausted_runners
from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_AGREED_NO,
    OUTCOME_DESIGN,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
    REASON_BOTH_PASS,
)
from cascade.review import CLAUDE_SOURCES
from cascade.review import label as reviewer_label
from cascade.rounds import Ledger
from cascade.runners import STATUS_AUTH
from cascade.workers import LOGIN_COMMANDS, WORKERS_CLAUDE, describe, fallback_counts

SOURCE_PR_COMMENT = "pr-comment"
BUCKET_REBUTTAL = "반박"
BUCKET_DESIGN = "설계 결정"
BUCKET_UNDECIDABLE = "판단 불가"
BUCKET_HOLD = "보류 후보"
BUCKETS = (BUCKET_REBUTTAL, BUCKET_DESIGN, BUCKET_UNDECIDABLE, BUCKET_HOLD)
ADJUDICATED_NO = "판가름: 아님"
ADJUDICATION_MARK = "판가름"
WORKER_ROLES = ("a", "b", "writer", "checker", "adjudicator")
OPINION_MAX_LINES = 20
COMMIT_SUBJECT_MAX = 60
REBUTTAL_CHOICE = "선택: [ ] 동의 [ ] 그래도 고친다"
HOLD_CHOICE = "선택: [ ] 지금 반영 [ ] 구현 때 테스트로 [ ] 보류"
DESIGN_CHOICE = "선택: [ ] 직접 정함 [ ] 보류"
DESIGN_CHOICE_WITH_OPTIONS = "선택: [ ] 선택지 ___ [ ] 직접 정함 [ ] 보류"
DESIGN_CHOICE_WITH_PATCH = "선택: [ ] 작성자 안 채택(패치 직접 적용) [ ] 직접 정함 [ ] 보류"
DRY_RUN_NOT_APPLIED = "적용 안 됨(dry-run)"
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


def _failed_ids(ledger: Ledger, run_dir: Path | None) -> dict[str, str]:
    failed: dict[str, str] = {}
    for rnd in ledger.rounds:
        for fid, tail in _failed_items(_load_apply(run_dir, rnd.no)):
            failed[fid] = tail
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
                out.append(f"  - {reviewer_label(ledger.reviewer)} 의견:")
                out.extend(f"    > {line}" for line in opinion.splitlines()[:OPINION_MAX_LINES])
            out.append(f"  - 패치 a: round-{round_no}/patch-{fid}-a.diff")
            out.append(f"  - 패치 b: round-{round_no}/patch-{fid}-b.diff")
        else:
            out.extend(_indent_reasons(decision.get("reasons", [])))
            out.append(f"  - {HOLD_CHOICE}")
            chosen = ledger.hold_choices.get(fid)
            if chosen:
                out.append(f"  - 선택됨: {chosen}")
    out.append("")
    return out


def _round_row(rnd, ledger: Ledger) -> str:
    consensus = sum(1 for d in rnd.decisions
                    if d["outcome"] in (OUTCOME_AGREED_FACT, OUTCOME_AGREED_NO) or d.get("adopted_as") == OUTCOME_AGREED_FACT)
    sent = sum(1 for d in rnd.decisions if _was_adjudicated(d))
    adjudicated = sum(1 for d in rnd.decisions
                      if (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from") is not None)
                      or d.get("adopted_as") == OUTCOME_ADJUDICATE)
    codex = sum(1 for f in rnd.findings if f.get("id") in ledger.opinions)
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


def _adopted_pairs(ledger: Ledger, failed: dict[str, str]) -> list[tuple[dict, dict]]:
    pairs = []
    for rnd in ledger.rounds:
        for d in rnd.decisions:
            if _is_adopted(d) and d["finding_id"] not in failed:
                pairs.append((_finding_of(rnd, d["finding_id"]), d))
    return pairs


def _commit_message(ledger: Ledger, failed: dict[str, str]) -> str:
    pairs = _adopted_pairs(ledger, failed)
    if not pairs:
        return ""
    if len(pairs) == 1:
        first = _first_line(pairs[0][0].get("body", ""))[:COMMIT_SUBJECT_MAX]
        subject = f"fix: {first}" if first else f"fix: 리뷰 지적 {pairs[0][0]['id']} 반영"
    else:
        subject = f"fix: {len(pairs)}건의 리뷰 지적 반영"
    bullets = [f"- {f['id']} {f.get('location', '-')}: {_first_line(f.get('body', ''))}" for f, _ in pairs]
    return "\n".join([subject, "", *bullets]) + "\n"


def _final_decisions(ledger: Ledger) -> dict[str, tuple[int, dict]]:
    final: dict[str, tuple[int, dict]] = {}
    for rnd in ledger.rounds:
        for d in rnd.decisions:
            final[d["finding_id"]] = (rnd.no, d)
    return final


def _reply_drafts(ledger: Ledger, run_dir: Path | None, failed: dict[str, str]) -> dict[str, str]:
    final = _final_decisions(ledger)
    drafts: dict[str, str] = {}
    for rnd in ledger.rounds:
        for f in rnd.findings:
            thread_id = f.get("thread_id")
            if not thread_id:
                continue
            entry = final.get(f["id"])
            if entry is None:
                drafts[thread_id] = "검토 중입니다. 미판정"
                continue
            round_no, d = entry
            reasons = d.get("reasons", [])
            if _is_adopted(d) and f["id"] in failed:
                drafts[thread_id] = f"적용 실패: {_first_line(failed[f['id']])}. 검토 중입니다."
            elif _is_adopted(d):
                drafts[thread_id] = " ".join(["반영했습니다.", *reasons[:1]]).strip()
            elif _bucket_of(d) == BUCKET_REBUTTAL:
                lines = _judge_lines(d, _load_results(run_dir, round_no))
                drafts[thread_id] = " ".join(["확인 결과", " / ".join(lines)]).strip()
            else:
                drafts[thread_id] = f"검토 중입니다. {d['outcome']}"
    return drafts


def _resolve_targets(ledger: Ledger, failed: dict[str, str]) -> list[str]:
    targets = []
    for f, _ in _adopted_pairs(ledger, failed):
        thread_id = f.get("thread_id")
        if thread_id and thread_id not in targets:
            targets.append(thread_id)
    return targets


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


def _calls_section(ledger: Ledger, run_dir: Path | None) -> list[str]:
    counts = {role: 0 for role in WORKER_ROLES}
    found = False
    for rnd in ledger.rounds:
        results = _load_results(run_dir, rnd.no)
        if results is None:
            continue
        found = True
        for key in results:
            role = key.rsplit(":", 1)[-1]
            if role in counts:
                counts[role] += 1
    out = ["## 호출 수와 시간", "", "| 역할 | 호출 수 |", "| --- | ---: |"]
    for role in WORKER_ROLES:
        out.append(f"| {role} | {counts[role] if found else '-'} |")
    name = reviewer_label(ledger.reviewer)
    if ledger.source in CLAUDE_SOURCES:
        out.append(f"| {name} 첫 검토 | 1 |")
    out.append(f"| {name} 의견 | {len(ledger.opinions)} |")
    out.append(f"| {name} 재검토 | {sum(1 for r in ledger.rounds if r.recheck_opened)} |")
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


def _workers_section(ledger: Ledger, run_dir: Path | None) -> list[str]:
    if ledger.workers_choice == WORKERS_CLAUDE or not ledger.workers:
        return []
    out = ["## 외부 작업자", "", f"배정: {describe(ledger.workers)}", ""]
    events = _worker_events(ledger)
    usage: dict[str, dict[str, int]] = {}
    for no, stage, event in events:
        total = sum((event.get("counts") or {}).values())
        n, reasons = fallback_counts(event.get("counts") or {})
        out.append(f"- 회차 {no} {stage}: CLI 작업 {total}건, 대체 실행 {n}건" + (f"({reasons})" if n else ""))
        for runner, tokens in (event.get("usage") or {}).items():
            sums = usage.setdefault(runner, {"input": 0, "output": 0, "cache_read": 0})
            for key in sums:
                sums[key] += tokens.get(key, 0)
    if usage:
        out.extend(["", "| CLI | 입력 토큰 | 출력 토큰 | 캐시 읽기 토큰 |", "| --- | ---: | ---: | ---: |"])
        out.extend(f"| {runner} | {t['input']} | {t['output']} | {t['cache_read']} |" for runner, t in usage.items())
    for runner in _auth_runners(ledger, run_dir):
        out.extend(["", f"{runner}: 로그인이 풀렸다. `{LOGIN_COMMANDS[runner]}` 로 다시 로그인한 뒤 이어서 돌린다."])
    out.append("")
    return out


def render(ledger: Ledger, dry_run: bool, run_dir: Path | None = None) -> str:
    run_dir = Path(run_dir) if run_dir is not None else None
    out = [f"# cascade-fix 보고서 {ledger.run_id}", ""]
    if dry_run:
        out.extend(["**적용하지 않음(dry-run)**", ""])
    out.extend([f"출처: {ledger.source} · 모드: {ledger.mode} · 검토자: {reviewer_label(ledger.reviewer)} · 상태: {ledger.status}", ""])
    out.extend(
        [
            "## 회차별 지적과 채택 경로",
            "",
            f"| 회차 | 지적 | 합의 | 판가름 회부 | 판가름 채택 | {reviewer_label(ledger.reviewer)} 검토 | 사람 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            *[_round_row(rnd, ledger) for rnd in ledger.rounds],
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
    out.extend(_calls_section(ledger, run_dir))
    out.extend(_workers_section(ledger, run_dir))
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
