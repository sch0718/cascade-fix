import argparse
import json
import sys
from pathlib import Path

TARGET_AGREEMENT = 0.95
TARGET_OPUS = 0.40
VERDICT_FACT = "사실"
VERDICT_NO = "아님"
VERDICT_UNREPRODUCIBLE = "재현 불가"
VERDICT_NONE = "판정 없음"
OUTCOME_AGREED = ("합의-사실", "합의-아님")
OUTCOME_ADJUDICATED = "판가름"
OUTCOMES_TO_HUMAN = ("판단불가", "설계결정", "보류후보")
REASON_PATCH_DIFFERS = "패치가 다름"
REASON_ADJUDICATED_NO = "판가름: 아님"
UNMEASURED = "측정 불가(dry-run은 전체 테스트를 돌리지 않음)"
PATH_KEYS = ("합의", "판가름", "Codex 검토", "사람")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_sample(path) -> dict[str, dict]:
    items = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            items[item["id"]] = item
    return items


def final_decision(ledger: dict, finding_id: str) -> dict | None:
    for rnd in reversed(ledger.get("rounds", [])):
        found = [d for d in rnd.get("decisions", []) if d.get("finding_id") == finding_id]
        if found:
            return found[-1]
    return None


def predict(decision: dict | None) -> str:
    if decision is None:
        return VERDICT_NONE
    outcome, reasons = decision.get("outcome"), decision.get("reasons", [])
    if outcome == "합의-사실" or (outcome == OUTCOME_ADJUDICATED and decision.get("patch_from")):
        return VERDICT_FACT
    if outcome == "합의-아님" or (outcome == OUTCOME_ADJUDICATED and REASON_ADJUDICATED_NO in reasons):
        return VERDICT_NO
    if any(VERDICT_UNREPRODUCIBLE in r for r in reasons):
        return VERDICT_UNREPRODUCIBLE
    return VERDICT_NONE


def path_of(decision: dict | None, opinions: dict, finding_id: str) -> str | None:
    if decision is None:
        return None
    outcome = decision.get("outcome")
    if outcome in OUTCOME_AGREED:
        return "합의"
    if outcome == OUTCOME_ADJUDICATED:
        return "판가름"
    if outcome in OUTCOMES_TO_HUMAN:
        return "Codex 검토" if finding_id in opinions else "사람"
    return None


def round_files(run_dir: Path, name: str):
    for path in sorted(run_dir.glob(f"round-*/{name}")):
        yield read_json(path)


def count_roles(run_dir: Path, role: str) -> list[dict]:
    out = []
    for merged in round_files(run_dir, "results.json"):
        out += [w for key, w in merged.items() if key.endswith(f":{role}")]
    return out


def checker_stats(ledger: dict, run_dir: Path) -> tuple[int, int]:
    if ledger.get("mode") != "doc":
        return 0, 0
    false_ids, fixed = 0, 0
    for merged in round_files(run_dir, "results.json"):
        for key, w in merged.items():
            if key.endswith(":checker") and w.get("checker_ok") is False:
                false_ids += 1
                adj = merged.get(f"{key.rpartition(':')[0]}:adjudicator")
                if adj and adj.get("verdict") == VERDICT_FACT:
                    fixed += 1
    return fixed, false_ids


def compute(runs, sample: dict[str, dict]) -> dict:
    runs = Path(runs)
    m = {"items": 0, "errors": 0, "incomplete": 0, "missing": 0, "regression_measured": False, "agree": 0, "judged": 0, "opus_calls": 0, "baseline_calls": 0,
         "patch_differs": 0, "regressions": 0, "paths": dict.fromkeys(PATH_KEYS, 0), "predicted": {}}
    checker_fixed = checker_false = 0
    for item_id, item in sample.items():
        base = runs / item_id
        if not (base / "meta.json").is_file():
            m["missing"] += 1
            continue
        meta = read_json(base / "meta.json")
        if "error" in meta:
            m["errors"] += 1
            continue
        if meta.get("incomplete"):
            m["incomplete"] += 1
            continue
        if not (base / "ledger.json").is_file():
            m["missing"] += 1
            continue
        ledger, run_dir = read_json(base / "ledger.json"), base / "run"
        fid = item["finding"]["id"]
        decision = final_decision(ledger, fid)
        verdict = predict(decision)
        m["items"] += 1
        m["predicted"][item_id] = verdict
        if verdict != VERDICT_NONE:
            m["judged"] += 1
            m["agree"] += verdict == item["truth"]["verdict"]
        path = path_of(decision, ledger.get("opinions", {}), fid)
        if path:
            m["paths"][path] += 1
        if any(REASON_PATCH_DIFFERS in r for rnd in ledger.get("rounds", []) for d in rnd.get("decisions", [])
               if d.get("finding_id") == fid for r in d.get("reasons", [])):
            m["patch_differs"] += 1
        m["opus_calls"] += len(count_roles(run_dir, "adjudicator"))
        m["baseline_calls"] += item.get("baseline_opus_calls", 1)
        applies = list(round_files(run_dir, "apply.json"))
        m["regression_measured"] |= any(a.get("final_ok") is not None for a in applies)
        m["regressions"] += any(a.get("final_ok") is False for a in applies)
        fixed, false_ids = checker_stats(ledger, run_dir)
        checker_fixed += fixed
        checker_false += false_ids
    m["opus_ratio"] = m["opus_calls"] / m["baseline_calls"] if m["baseline_calls"] else None
    m["agreement"] = m["agree"] / m["judged"] if m["judged"] else None
    m["checker"] = (checker_fixed, checker_false)
    return m


def pct(num: int, den: int) -> str:
    return f"{num}/{den} ({num / den * 100:.1f}%)" if den else "-"


def ratio_text(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "-"


def passed(ok: bool) -> str:
    return "통과" if ok else "미달"


def regression_text(m: dict) -> str:
    return f"회귀 {m['regressions']}건" if m["regression_measured"] else "회귀 미측정"


def verdict_line(m: dict) -> str:
    agree_ok = m["agreement"] is not None and m["agreement"] >= TARGET_AGREEMENT
    opus_ok = m["opus_ratio"] is not None and m["opus_ratio"] <= TARGET_OPUS
    return (f"성공 기준: 판정 일치 {ratio_text(m['agreement'])} (목표 95%) {passed(agree_ok)}"
            f" · Opus 호출 {ratio_text(m['opus_ratio'])} (목표 40% 이하) {passed(opus_ok)}"
            f" · {regression_text(m)} (목표 0)" + ("" if not m["regression_measured"] else f" {passed(m['regressions'] == 0)}"))


def render(m: dict) -> str:
    paths = ", ".join(f"{k} {v}" for k, v in m["paths"].items())
    rows = [
        ("판정 일치율", f"{pct(m['agree'], m['judged'])} — 일치/판정 있음"),
        ("채택 경로 분포", paths),
        ("패치 다르지만 둘 다 통과", pct(m["patch_differs"], m["items"])),
        ("Opus 호출 수", str(m["opus_calls"])),
        ("기준 Opus 호출 수", str(m["baseline_calls"])),
        ("Opus 호출 비율", ratio_text(m["opus_ratio"])),
        ("회귀 건수", str(m["regressions"]) if m["regression_measured"] else UNMEASURED),
        ("문서 점검자 정확도", pct(*m["checker"])),
        ("채점한 항목 수", str(m["items"])),
        ("오류 항목 수", str(m["errors"])),
        ("미완료 항목 수", str(m["incomplete"])),
        ("실행 기록 없는 항목 수", str(m["missing"])),
    ]
    lines = ["| 값 | 결과 |", "| --- | --- |", *(f"| {k} | {v} |" for k, v in rows), "", verdict_line(m), ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="소급 재생 결과 채점")
    parser.add_argument("--runs", required=True)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    text = render(compute(args.runs, load_sample(args.sample)))
    print(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
