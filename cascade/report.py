import json
from datetime import datetime
from pathlib import Path

from cascade.consensus import (
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_AGREED_NO,
    OUTCOME_DESIGN,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
)
from cascade.rounds import Ledger

SOURCE_PR_COMMENT = "pr-comment"
BUCKET_REBUTTAL = "반박"
BUCKET_DESIGN = "설계 결정"
BUCKET_UNDECIDABLE = "판단 불가"
BUCKET_HOLD = "보류 후보"
BUCKETS = (BUCKET_REBUTTAL, BUCKET_DESIGN, BUCKET_UNDECIDABLE, BUCKET_HOLD)
ADJUDICATED_NO = "판가름: 아님"
WORKER_ROLES = ("a", "b", "writer", "checker", "adjudicator")
OPINION_MAX_LINES = 20
COMMIT_SUBJECT_MAX = 60
REBUTTAL_CHOICE = "선택: [ ] 동의 [ ] 그래도 고친다"
HOLD_CHOICE = "선택: [ ] 지금 반영 [ ] 구현 때 테스트로 [ ] 보류"
TABLE_FOOTNOTE = (
    "합의-아님은 합의 칸에 한 번 세고 사람이 볼 목록의 반박 칸에도 오른다. "
    "사람 칸은 설계결정·판단불가·보류후보와 판가름 뒤에도 채택하지 못한 지적이다."
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
    items = []
    for entry in (apply or {}).get("failed", []) or []:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
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
    return (
        f"- **{finding.get('id', decision['finding_id'])}** "
        f"({finding.get('severity', '-')}, {finding.get('location', '-')}) — {_first_line(finding.get('body', ''))}"
    )


def _rebuttal_lines(decision: dict, results: dict | None) -> list[str]:
    fid = decision["finding_id"]
    a = _worker_reason(results, fid, "a")
    b = _worker_reason(results, fid, "b")
    if a and b:
        extra = [r for r in decision.get("reasons", []) if not r.startswith(("a:", "b:"))]
        return [f"a: {a}", f"b: {b}", *extra]
    return list(decision.get("reasons", []))


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
            out.extend(_indent_reasons(_rebuttal_lines(decision, results)))
            out.append(f"  - {REBUTTAL_CHOICE}")
        elif name == BUCKET_DESIGN:
            out.extend(_indent_reasons(decision.get("reasons", [])))
        elif name == BUCKET_UNDECIDABLE:
            out.extend(_indent_reasons(decision.get("reasons", [])))
            opus = _worker_reason(results, fid, "adjudicator")
            if opus:
                out.append(f"  - Opus: {opus}")
            opinion = ledger.opinions.get(fid)
            if opinion:
                out.append("  - Codex 의견:")
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
    consensus = sum(1 for d in rnd.decisions if d["outcome"] in (OUTCOME_AGREED_FACT, OUTCOME_AGREED_NO))
    adjudicated = sum(1 for d in rnd.decisions if d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from") is not None)
    codex = sum(1 for f in rnd.findings if f.get("id") in ledger.opinions)
    human = sum(
        1
        for d in rnd.decisions
        if d["outcome"] in (OUTCOME_DESIGN, OUTCOME_UNDECIDABLE, OUTCOME_HOLD)
        or (d["outcome"] == OUTCOME_ADJUDICATE and d.get("patch_from") is None)
    )
    return f"| {rnd.no} | {len(rnd.findings)} | {consensus} | {adjudicated} | {codex} | {human} |"


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
                lines = _rebuttal_lines(d, _load_results(run_dir, round_no))
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
    if ledger.source == SOURCE_PR_COMMENT:
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
    times = [t for t in (_parse_time(e.get("at", "")) for e in rnd.events) if t is not None]
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
    out.append(f"| Codex 검토 | {len(ledger.opinions)} |")
    out.append(f"| Codex 재검토 | {sum(1 for r in ledger.rounds if r.recheck_opened)} |")
    out.extend(["", "| 회차 | 소요 |", "| --- | ---: |"])
    out.extend(f"| {rnd.no} | {_elapsed(rnd)} |" for rnd in ledger.rounds)
    out.append("")
    return out


def render(ledger: Ledger, dry_run: bool, run_dir: Path | None = None) -> str:
    run_dir = Path(run_dir) if run_dir is not None else None
    out = [f"# cascade-fix 보고서 {ledger.run_id}", ""]
    if dry_run:
        out.extend(["**적용하지 않음(dry-run)**", ""])
    out.extend([f"출처: {ledger.source} · 모드: {ledger.mode} · 상태: {ledger.status}", ""])
    out.extend(
        [
            "## 회차별 지적과 채택 경로",
            "",
            "| 회차 | 지적 | 합의 | 판가름 | Codex 검토 | 사람 |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
            *[_round_row(rnd, ledger) for rnd in ledger.rounds],
            "",
            TABLE_FOOTNOTE,
            "",
            "## 사람이 볼 목록",
            "",
        ]
    )
    for name, items in _bucketed(ledger).items():
        out.extend(_bucket_section(name, items, ledger, run_dir))
    out.extend(_prepared_section(ledger, run_dir))
    out.extend(_calls_section(ledger, run_dir))
    return "\n".join(out).rstrip("\n") + "\n"


def write_prepared(run_dir: Path, ledger: Ledger) -> None:
    prepared = Path(run_dir) / "prepared"
    prepared.mkdir(parents=True, exist_ok=True)
    failed = _failed_ids(ledger, Path(run_dir))
    (prepared / "commit-message.txt").write_text(_commit_message(ledger, failed), encoding="utf-8")
    replies = _reply_drafts(ledger, Path(run_dir), failed) if ledger.source == SOURCE_PR_COMMENT else {}
    (prepared / "replies.json").write_text(json.dumps(replies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (prepared / "resolve.json").write_text(
        json.dumps(_resolve_targets(ledger, failed) if ledger.source == SOURCE_PR_COMMENT else [], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
