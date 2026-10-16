from dataclasses import asdict, dataclass, field, fields

from cascade.patches import same
from cascade.verify import VerifyResult

VERDICT_FACT = "사실"
VERDICT_NO = "아님"
VERDICT_UNREPRODUCIBLE = "재현 불가"
VERDICT_UNDECIDED = "못 정함"

OUTCOME_AGREED_FACT = "합의-사실"
OUTCOME_AGREED_NO = "합의-아님"
OUTCOME_ADJUDICATE = "판가름"
OUTCOME_DESIGN = "설계결정"
OUTCOME_UNDECIDABLE = "판단불가"
OUTCOME_HOLD = "보류후보"

REASON_SENSITIVE = "보안 경로"
REASON_NO_VERIFIER = "검증기 없음"
REASON_SENSITIVE_NO_ADOPT = "보안 경로: 자동 채택 안 함"
VERDICTS = (VERDICT_FACT, VERDICT_NO, VERDICT_UNREPRODUCIBLE, VERDICT_UNDECIDED)


@dataclass
class WorkerResult:
    finding_id: str
    role: str
    verdict: str
    needs_design_decision: bool
    reason: str
    verify: list[dict]
    checker_ok: bool | None
    model_id: str
    patch: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerResult":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Decision:
    finding_id: str
    outcome: str
    patch_from: str | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Decision":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def _all_ok(results: list[VerifyResult]) -> bool:
    return all(r.ok for r in results)


def _decision(a: WorkerResult, outcome: str, reasons: list[str], patch_from: str | None = None) -> Decision:
    return Decision(finding_id=a.finding_id, outcome=outcome, patch_from=patch_from, reasons=reasons)


def _check_verdict(w: WorkerResult) -> None:
    if w.verdict not in VERDICTS:
        raise ValueError(f"알 수 없는 판정: {w.verdict!r} (role={w.role})")


def _agree_on_verdict(a: WorkerResult, b: WorkerResult) -> Decision | None:
    _check_verdict(a)
    _check_verdict(b)
    if a.verdict == VERDICT_UNDECIDED and b.verdict == VERDICT_UNDECIDED:
        return _decision(a, OUTCOME_UNDECIDABLE, ["호출 실패"])
    if a.verdict != b.verdict:
        return _decision(a, OUTCOME_ADJUDICATE, [f"판정이 갈림: a={a.verdict}, b={b.verdict}"])
    if a.verdict == VERDICT_NO:
        return _decision(a, OUTCOME_AGREED_NO, [f"a: {a.reason}", f"b: {b.reason}"])
    if a.verdict == VERDICT_UNREPRODUCIBLE:
        return _decision(a, OUTCOME_ADJUDICATE, ["둘 다 재현 불가"])
    if a.needs_design_decision and b.needs_design_decision:
        return _decision(a, OUTCOME_DESIGN, [])
    if a.needs_design_decision != b.needs_design_decision:
        return _decision(a, OUTCOME_ADJUDICATE, ["설계 결정 필요 여부 갈림"])
    return None


def _flag_sensitive(d: Decision, sensitive: bool) -> Decision:
    if not sensitive:
        return d
    reasons = [*d.reasons, REASON_SENSITIVE]
    if d.outcome == OUTCOME_AGREED_FACT:
        return Decision(finding_id=d.finding_id, outcome=OUTCOME_UNDECIDABLE, patch_from=None, reasons=reasons)
    return Decision(finding_id=d.finding_id, outcome=d.outcome, patch_from=d.patch_from, reasons=reasons)


def decide(
    a: WorkerResult,
    b: WorkerResult,
    va: list[VerifyResult],
    vb: list[VerifyResult],
    pa: str,
    pb: str,
    sensitive: bool = False,
) -> Decision:
    d = _agree_on_verdict(a, b)
    if d is None:
        d = _decide_facts(a, va, vb, pa, pb)
    return _flag_sensitive(d, sensitive)


def _decide_facts(a: WorkerResult, va: list[VerifyResult], vb: list[VerifyResult], pa: str, pb: str) -> Decision:
    notes = [REASON_NO_VERIFIER] if not va or not vb else []
    a_ok = _all_ok(va)
    b_ok = _all_ok(vb)
    if a_ok and b_ok:
        if same(pa, pb):
            return _decision(a, OUTCOME_AGREED_FACT, notes, "a")
        return _decision(a, OUTCOME_ADJUDICATE, ["패치가 다름", *notes])
    if a_ok or b_ok:
        return _decision(a, OUTCOME_ADJUDICATE, ["한쪽만 검증 통과", *notes], "a" if a_ok else "b")
    return _decision(a, OUTCOME_ADJUDICATE, ["둘 다 검증 실패"])


def decide_doc(
    judge_a: WorkerResult,
    judge_b: WorkerResult,
    checker: WorkerResult | None,
    refs_missing: list[str],
) -> Decision:
    d = _agree_on_verdict(judge_a, judge_b)
    if d is not None:
        return d
    if refs_missing:
        return _decision(judge_a, OUTCOME_ADJUDICATE, [f"참조 없음: {', '.join(refs_missing)}"])
    if checker is None:
        return _decision(judge_a, OUTCOME_ADJUDICATE, ["점검자 없음"])
    if checker.checker_ok is None:
        return _decision(judge_a, OUTCOME_ADJUDICATE, ["점검자 결과 없음"])
    if checker.checker_ok is False:
        return _decision(judge_a, OUTCOME_ADJUDICATE, ["점검자 아니오"])
    return _decision(judge_a, OUTCOME_AGREED_FACT, [], "writer")


def after_adjudication(d: Decision, adj: WorkerResult, vadj: list[VerifyResult]) -> Decision:
    if d.outcome != OUTCOME_ADJUDICATE:
        raise ValueError(f"판가름 단계가 아닌 결정에는 쓸 수 없다: {d.outcome}")
    _check_verdict(adj)
    note = f"판가름: {adj.reason}"

    def result(outcome: str, patch_from: str | None, *extra: str) -> Decision:
        return Decision(finding_id=d.finding_id, outcome=outcome, patch_from=patch_from, reasons=[*d.reasons, *extra, note])

    if adj.verdict == VERDICT_UNDECIDED:
        return result(OUTCOME_UNDECIDABLE, None, "판가름 못 정함")
    if adj.verdict == VERDICT_UNREPRODUCIBLE:
        return result(OUTCOME_UNDECIDABLE, None, "판가름 재현 불가")
    if adj.verdict == VERDICT_NO:
        return result(OUTCOME_ADJUDICATE, None, "판가름: 아님")
    if adj.needs_design_decision:
        return result(OUTCOME_DESIGN, None)
    if REASON_SENSITIVE in d.reasons:
        return result(OUTCOME_UNDECIDABLE, None, REASON_SENSITIVE_NO_ADOPT)
    if not _all_ok(vadj):
        return result(OUTCOME_UNDECIDABLE, None, "판가름 패치 검증 실패")
    if not adj.patch and d.patch_from in ("a", "b"):
        return result(OUTCOME_ADJUDICATE, d.patch_from)
    return result(OUTCOME_ADJUDICATE, "adjudicator")
