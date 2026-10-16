import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from cascade.consensus import WorkerResult
from cascade.findings import Finding, parse_codex_doc
from cascade.jobs import (
    AdjInput,
    _finding_values,
    _format_patch,
    _format_result,
    _format_verify,
    _render,
)

STATUS_RUNNING = "진행"
STATUS_DONE = "종료"
DEFAULT_MAX_RECHECKS = 2
CONVERGED_MAX = 2
RESOLUTION_HEADER = "## 해소 여부"

_MODE_NOTES = {
    "doc": "문서의 바뀐 절과 그 수정이 지적을 해소했는지만 본다.",
    "code": "수정 커밋 범위의 diff만 본다.",
}


@dataclass
class RoundRecord:
    no: int
    findings: list[dict]
    decisions: list[dict]
    adopted: list[str]
    recheck_opened: bool
    final: bool = False
    started_at: str = ""
    events: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "RoundRecord":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Ledger:
    run_id: str
    source: str
    mode: str
    root: str
    snapshot: dict
    dry_run: bool
    max_rechecks: int = DEFAULT_MAX_RECHECKS
    rounds: list[RoundRecord] = field(default_factory=list)
    status: str = STATUS_RUNNING
    hold_choices: dict[str, str] = field(default_factory=dict)
    opinions: dict[str, str] = field(default_factory=dict)
    docs: list[str] = field(default_factory=list)

    def current_round(self) -> RoundRecord | None:
        return self.rounds[-1] if self.rounds else None

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        names = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in names}
        values["rounds"] = [RoundRecord.from_dict(r) for r in values.get("rounds", [])]
        return cls(**values)


def ledger_path(run_dir: Path) -> Path:
    return Path(run_dir) / "ledger.json"


def needs_recheck(round: RoundRecord, max_rechecks: int, rechecks_done: int) -> bool:
    if round.final or rechecks_done >= max_rechecks:
        return False
    adopted = set(round.adopted)
    return any(
        f.get("severity") in ("Critical", "Warning") for f in round.findings if f.get("id") in adopted
    )


def converged(new_findings: list[Finding]) -> bool:
    return len(new_findings) <= CONVERGED_MAX and not any(f.severity == "Critical" for f in new_findings)


def repeated_critical(prev: list[Finding], new: list[Finding]) -> list[Finding]:
    prev_locations = {f.location for f in prev if f.severity == "Critical"}
    return [f for f in new if f.severity == "Critical" and f.location in prev_locations]


def classify_recheck(
    new: list[Finding], prev: RoundRecord, max_reached: bool, final: bool = False
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    if final:
        undecidable = [f for f in new if f.severity == "Critical"]
        return [], [f for f in new if f.severity != "Critical"], undecidable
    repeats = repeated_critical([Finding.from_dict(d) for d in prev.findings], new)
    repeat_ids = {f.id for f in repeats}
    hold = [f for f in new if f.id not in repeat_ids and f.severity == "Suggestion"]
    remaining = [f for f in new if f.id not in repeat_ids and f.severity != "Suggestion"]
    if any(f.severity == "Critical" for f in remaining):
        return remaining, hold, repeats
    if max_reached or converged(remaining):
        return [], hold + remaining, repeats
    return remaining, hold, repeats


def _fence(text: str, lang: str = "") -> str:
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text.rstrip(chr(10))}\n{fence}"


def _findings_list(findings: list[Finding]) -> str:
    lines = []
    for i, f in enumerate(findings, 1):
        first = f.body.strip().splitlines()[0] if f.body.strip() else ""
        lines.append(f"{i}. {f.id} [{f.severity}] {f.location} — {first}")
    return "\n".join(lines) if lines else "(없음)"


def build_recheck_prompt(findings: list[Finding], diff: str, mode: str) -> str:
    return _render("recheck.md", {
        "findings": _findings_list(findings),
        "diff": _fence(diff, "diff") if diff.strip() else "(변경 없음)",
        "mode_note": _MODE_NOTES.get(mode, _MODE_NOTES["code"]),
    })


def parse_recheck(text: str, round_no: int) -> tuple[dict[str, bool], list[Finding]]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == RESOLUTION_HEADER), None)
    resolved: dict[str, bool] = {}
    rest = lines
    if start is not None:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not stripped.startswith("-"):
                end = i
                break
            body = stripped[1:].strip()
            fid, sep, verdict = body.partition(":")
            if not sep:
                continue
            verdict = verdict.strip()
            if verdict.startswith("해소 안 됨"):
                resolved[fid.strip()] = False
            elif verdict.startswith("해소됨"):
                resolved[fid.strip()] = True
        rest = lines[end:]
    return resolved, parse_codex_doc("\n".join(rest), round_no)


def build_opinion_prompt(item: AdjInput, adj: WorkerResult) -> str:
    values = _finding_values(item.finding, "opinion", item.worktree)
    values.update({
        "mode": item.mode,
        "result_a": _format_result(item.a) + "\n\n검증 결과:\n\n" + _format_verify(item.verify_a),
        "result_b": _format_result(item.b) + "\n\n검증 결과:\n\n" + _format_verify(item.verify_b),
        "patch_a": _format_patch(item.patch_a),
        "patch_b": _format_patch(item.patch_b),
        "result_adj": _format_result(adj) + "\n\n검증 결과:\n\n" + _format_verify(adj.verify),
        "patch_adj": _format_patch(adj.patch),
    })
    return _render("opinion.md", values)
