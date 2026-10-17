import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import report  # noqa: E402
from cascade.consensus import (  # noqa: E402
    OUTCOME_ADJUDICATE,
    OUTCOME_AGREED_FACT,
    OUTCOME_AGREED_NO,
    OUTCOME_DESIGN,
    OUTCOME_HOLD,
    OUTCOME_UNDECIDABLE,
)
from cascade.rounds import Ledger, RoundRecord  # noqa: E402
from helpers import git, make_repo  # noqa: E402


def finding(fid, severity="Warning", location="src/a.py:10", body="첫 줄 본문\n둘째 줄", thread_id=None):
    return {
        "id": fid,
        "source": "review",
        "severity": severity,
        "location": location,
        "body": body,
        "evidence": "",
        "thread_id": thread_id,
        "outdated": False,
    }


def decision(fid, outcome, patch_from=None, reasons=None):
    return {"finding_id": fid, "outcome": outcome, "patch_from": patch_from, "reasons": reasons or []}


def worker(fid, role, reason="근거"):
    return {
        "finding_id": fid,
        "role": role,
        "verdict": "사실",
        "needs_design_decision": False,
        "reason": reason,
        "verify": [],
        "checker_ok": None,
        "model_id": "m",
        "patch": "",
    }


def make_ledger(findings, decisions, source="local", dry_run=False, opinions=None, events=None, recheck=False):
    adopted = [
        d["finding_id"]
        for d in decisions
        if d["outcome"] == OUTCOME_AGREED_FACT or (d["outcome"] == OUTCOME_ADJUDICATE and d["patch_from"])
    ]
    rnd = RoundRecord(
        no=1,
        findings=findings,
        decisions=decisions,
        adopted=adopted,
        recheck_opened=recheck,
        events=events or [],
    )
    return Ledger(
        run_id="run-1",
        source=source,
        mode="code",
        root="/tmp/x",
        snapshot={},
        dry_run=dry_run,
        rounds=[rnd],
        opinions=opinions or {},
    )


def section(text, heading):
    start = text.index(heading)
    nxt = text.find("\n## ", start + 1) if heading.startswith("## ") else text.find("\n### ", start + 1)
    return text[start:] if nxt == -1 else text[start:nxt]


def full_ledger():
    findings = [
        finding("F1", location="a.py:1", body="합의 사실"),
        finding("F2", location="b.py:2", body="판가름 채택"),
        finding("F3", location="c.py:3", body="합의 아님"),
        finding("F4", location="d.py:4", body="설계 필요"),
        finding("F5", location="e.py:5", body="판단 못 함"),
        finding("F6", location="f.py:6", body="보류 대상", severity="Suggestion"),
        finding("F7", location="g.py:7", body="판가름 아님"),
    ]
    decisions = [
        decision("F1", OUTCOME_AGREED_FACT, "a", ["둘 다 통과"]),
        decision("F2", OUTCOME_ADJUDICATE, "b", ["한쪽만 검증 통과", "판가름: 사실"]),
        decision("F3", OUTCOME_AGREED_NO, None, ["a: 코드에 없음", "b: 이미 처리됨"]),
        decision("F4", OUTCOME_DESIGN, None, ["API 형태를 정해야 함"]),
        decision("F5", OUTCOME_UNDECIDABLE, None, ["패치가 다름"]),
        decision("F6", OUTCOME_HOLD, None, ["제안 수준"]),
        decision("F7", OUTCOME_ADJUDICATE, None, ["패치가 다름", "판가름: 아님 — 이미 있음"]),
    ]
    return make_ledger(findings, decisions, opinions={"F5": "Codex 의견 1\nCodex 의견 2"})


class RenderTest(unittest.TestCase):
    def test_render_separates_consensus_and_adjudicated(self):
        text = report.render(full_ledger(), dry_run=False)
        table = section(text, "## 회차별 지적과 채택 경로")
        self.assertIn("| 회차 | 지적 | 합의 | 판가름 회부 | 판가름 채택 | Codex 검토 | 사람 |", table)
        self.assertIn("| 1 | 7 | 2 | 2 | 1 | 1 | 4 |", table)
        self.assertIn("합의-아님", table)
        commit = section(text, "### 커밋 메시지 초안")
        self.assertIn("- F2 b.py:2: 판가름 채택", commit)
        rebuttal = section(text, "### 반박")
        self.assertIn("**F3**", rebuttal)
        self.assertIn("**F7**", rebuttal)
        self.assertIn("판가름", rebuttal)

    def test_adjudication_sent_counts_findings_rewritten_after_adjudication(self):
        findings = [finding("R1"), finding("R2"), finding("R3")]
        decisions = [
            decision("R1", OUTCOME_UNDECIDABLE, None, ["판정이 갈림: a=사실, b=못 정함", "판가름 못 정함", "판가름: 원문 없음"]),
            decision("R2", OUTCOME_ADJUDICATE, None, ["판정이 갈림: a=아님, b=사실", "판가름: 아님", "판가름: 원문 그대로"]),
            decision("R3", OUTCOME_DESIGN, None, ["설계 결정 필요 여부 갈림", "판가름: 작성자가 정한다"]),
        ]
        text = report.render(make_ledger(findings, decisions, opinions={"R1": "의견"}), dry_run=True)
        table = section(text, "## 회차별 지적과 채택 경로")
        self.assertIn("| 1 | 3 | 0 | 3 | 0 | 1 | 3 |", table)

    def test_table_counts_decision_path_even_after_apply_failed(self):
        findings = [finding("A1"), finding("A2"), finding("A3")]
        decisions = [
            decision("A1", OUTCOME_AGREED_FACT, "a"),
            {**decision("A2", OUTCOME_UNDECIDABLE, None, ["패치 적용 실패: error: patch failed"]),
             "adopted_as": OUTCOME_AGREED_FACT},
            {**decision("A3", OUTCOME_UNDECIDABLE, None, ["한쪽만 검증 통과", "판가름: 고친다", "패치 적용 실패: x"]),
             "adopted_as": OUTCOME_ADJUDICATE},
        ]
        text = report.render(make_ledger(findings, decisions), dry_run=False)
        table = section(text, "## 회차별 지적과 채택 경로")
        self.assertIn("| 1 | 3 | 2 | 1 | 1 | 0 | 2 |", table)
        self.assertIn("판정 때 값", table)

    def test_four_buckets_present_with_reasons(self):
        text = report.render(full_ledger(), dry_run=False)
        people = section(text, "## 사람이 볼 목록")
        for heading in ("### 반박", "### 설계 결정", "### 판단 불가", "### 보류 후보"):
            self.assertIn(heading, people)
        rebuttal = section(text, "### 반박")
        self.assertIn("a: 코드에 없음", rebuttal)
        self.assertIn("b: 이미 처리됨", rebuttal)
        self.assertIn("선택: [ ] 동의 [ ] 그래도 고친다", rebuttal)
        self.assertIn("- **F3** (Warning, c.py:3) — 합의 아님", rebuttal)
        design = section(text, "### 설계 결정")
        self.assertIn("**F4**", design)
        self.assertIn("API 형태를 정해야 함", design)
        undecidable = section(text, "### 판단 불가")
        self.assertIn("Codex 의견 1", undecidable)
        self.assertIn("round-1/patch-F5-a.diff", undecidable)
        self.assertIn("round-1/patch-F5-b.diff", undecidable)
        hold = section(text, "### 보류 후보")
        self.assertIn("선택: [ ] 지금 반영 [ ] 구현 때 테스트로 [ ] 보류", hold)

    def test_hold_choice_shown_and_codex_opinion_capped(self):
        ledger = full_ledger()
        ledger.hold_choices = {"F6": "보류"}
        ledger.opinions["F5"] = "\n".join(f"줄{i}" for i in range(30))
        text = report.render(ledger, dry_run=False)
        self.assertIn("선택됨: 보류", section(text, "### 보류 후보"))
        undecidable = section(text, "### 판단 불가")
        self.assertIn("줄19", undecidable)
        self.assertNotIn("줄20", undecidable)

    def test_results_json_supplies_worker_and_adjudicator_reasons(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {
                "F3:a": worker("F3", "a", "A 근거 상세"),
                "F3:b": worker("F3", "b", "B 근거 상세"),
                "F5:adjudicator": worker("F5", "adjudicator", "Opus가 못 정함"),
            }
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, dry_run=False, run_dir=run_dir)
        self.assertIn("a: A 근거 상세", section(text, "### 반박"))
        self.assertIn("b: B 근거 상세", section(text, "### 반박"))
        self.assertIn("Opus가 못 정함", section(text, "### 판단 불가"))

    def test_adjudicator_reason_shown_once_when_already_in_reasons(self):
        ledger = full_ledger()
        ledger.rounds[0].decisions[4]["reasons"] = ["패치가 다름", "판가름 패치 검증 실패", "판가름: Opus가 고친 패치도 깨진다"]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {"F5:adjudicator": worker("F5", "adjudicator", "Opus가 고친 패치도 깨진다\n")}
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, dry_run=False, run_dir=run_dir)
        undecidable = section(text, "### 판단 불가")
        self.assertEqual(undecidable.count("Opus가 고친 패치도 깨진다"), 1)
        self.assertIn("  - 판가름: Opus가 고친 패치도 깨진다", undecidable)
        self.assertNotIn("Opus: ", undecidable)

    def test_dry_run_banner(self):
        ledger = full_ledger()
        text = report.render(ledger, dry_run=True)
        body_lines = text.splitlines()
        self.assertTrue(body_lines[0].startswith("# cascade-fix 보고서 run-1"))
        first_body = next(line for line in body_lines[1:] if line.strip() and not line.startswith("출처:"))
        self.assertEqual(first_body, "**적용하지 않음(dry-run)**")
        self.assertNotIn("적용하지 않음(dry-run)", report.render(ledger, dry_run=False))

    def test_header_line(self):
        text = report.render(full_ledger(), dry_run=False)
        self.assertIn("출처: local · 모드: code · 검토자: Codex · 상태: 진행", text)

    def test_commit_message_single_and_multiple(self):
        one = make_ledger(
            [finding("F1", body="x" * 80 + "\n나머지")],
            [decision("F1", OUTCOME_AGREED_FACT, "a")],
        )
        block = section(report.render(one, False), "### 커밋 메시지 초안")
        self.assertIn("fix: " + "x" * 60 + "\n", block)
        subject = next(line for line in block.splitlines() if line.startswith("fix: "))
        self.assertEqual(len(subject), len("fix: ") + 60)
        many = report.render(full_ledger(), False)
        block = section(many, "### 커밋 메시지 초안")
        self.assertIn("fix: 2건의 리뷰 지적 반영", block)
        self.assertIn("- F1 a.py:1: 합의 사실", block)

    def test_apply_result_section(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self.assertNotIn("### 적용 결과", report.render(ledger, False, run_dir=run_dir))
            (run_dir / "round-1").mkdir()
            (run_dir / "round-1" / "apply.json").write_text(
                json.dumps({"applied": ["F1"], "failed": [["F2", "충돌 꼬리"]]}, ensure_ascii=False), encoding="utf-8"
            )
            text = report.render(ledger, False, run_dir=run_dir)
        apply_section = section(text, "### 적용 결과")
        self.assertIn("F1", apply_section)
        self.assertIn("F2", apply_section)
        self.assertIn("충돌 꼬리", apply_section)

    def test_carried_finding_not_listed_as_failure(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            (run_dir / "round-1" / "apply.json").write_text(json.dumps(
                {"applied": ["F1"], "failed": [["F2", "겹침 꼬리"], ["F5", "진짜 실패"]], "carried": ["F2"]},
                ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, False, run_dir=run_dir)
        apply_section = section(text, "### 적용 결과")
        self.assertIn("- 회차 1 다음 회차로 넘김(앞 패치와 겹침): F2", apply_section)
        self.assertNotIn("실패 **F2**", apply_section)
        self.assertNotIn("겹침 꼬리", apply_section)
        self.assertIn("실패 **F5**", apply_section)

    def test_pr_comment_replies_and_resolve(self):
        findings = [
            finding("F1", thread_id="T1", body="고칠 것"),
            finding("F3", thread_id="T3", body="아님 건"),
            finding("F4", thread_id="T4", body="설계"),
            finding("F5", thread_id=None, body="스레드 없음"),
        ]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["둘 다 통과"]),
            decision("F3", OUTCOME_AGREED_NO, None, ["a: 코드에 없음", "b: 이미 처리됨"]),
            decision("F4", OUTCOME_DESIGN, None, []),
            decision("F5", OUTCOME_AGREED_FACT, "a", []),
        ]
        ledger = make_ledger(findings, decisions, source="pr-comment")
        text = report.render(ledger, False)
        replies = section(text, "### PR 답글 초안")
        self.assertIn("thread T1:", replies)
        self.assertIn("반영했습니다.", replies)
        self.assertIn("thread T3:", replies)
        self.assertIn("확인 결과", replies)
        self.assertIn("코드에 없음", replies)
        self.assertIn("thread T4:", replies)
        self.assertIn("검토 중입니다. 설계결정", replies)
        self.assertNotIn("F5", replies)
        resolve = section(text, "### resolve 대상")
        self.assertIn("T1", resolve)
        self.assertNotIn("T3", resolve)
        self.assertNotIn("T4", resolve)
        local = report.render(make_ledger(findings, decisions, source="local"), False)
        self.assertNotIn("### PR 답글 초안", local)
        self.assertNotIn("### resolve 대상", local)

    def _failed_apply_ledger(self):
        findings = [finding("F1", thread_id="T1", body="고칠 것 1"), finding("F2", thread_id="T2", body="고칠 것 2")]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["통과"]),
            decision("F2", OUTCOME_AGREED_FACT, "a", ["통과"]),
        ]
        return make_ledger(findings, decisions, source="pr-comment")

    def _write_apply(self, run_dir, payload):
        (run_dir / "round-1").mkdir(exist_ok=True)
        (run_dir / "round-1" / "apply.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_apply_failure_excluded_from_prepared_files(self):
        ledger = self._failed_apply_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self._write_apply(run_dir, {"applied": ["F1"], "failed": [["F2", "충돌 발생\n둘째 줄"]]})
            report.write_prepared(run_dir, ledger)
            message = (run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8")
            replies = json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8"))
            resolve = json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8"))
            text = report.render(ledger, False, run_dir=run_dir)
        self.assertEqual([line for line in message.splitlines() if line.startswith("- ")], ["- F1 src/a.py:10: 고칠 것 1"])
        self.assertTrue(message.startswith("fix: 고칠 것 1\n"))
        self.assertEqual(resolve, ["T1"])
        self.assertTrue(replies["T1"].startswith("반영했습니다."))
        self.assertEqual(replies["T2"], "적용 실패: 충돌 발생. 검토 중입니다.")
        self.assertIn("적용 실패: 충돌 발생. 검토 중입니다.", section(text, "### PR 답글 초안"))
        self.assertNotIn("T2", section(text, "### resolve 대상"))
        self.assertNotIn("- F2 ", section(text, "### 커밋 메시지 초안"))

    def test_malformed_apply_failed_entries_are_skipped(self):
        ledger = self._failed_apply_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            self._write_apply(run_dir, {"applied": ["F1"], "failed": ["F2", ["F2"], 5, None]})
            text = report.render(ledger, False, run_dir=run_dir)
            report.write_prepared(run_dir, ledger)
            resolve = json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8"))
        self.assertIn("### 적용 결과", text)
        self.assertEqual(resolve, ["T1", "T2"])

    def test_empty_worker_reason_falls_back_to_decision_reasons(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {"F3:a": worker("F3", "a", ""), "F3:b": worker("F3", "b", "B 근거")}
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            text = report.render(ledger, False, run_dir=run_dir)
        rebuttal = section(text, "### 반박")
        self.assertIn("a: 코드에 없음", rebuttal)
        self.assertIn("b: 이미 처리됨", rebuttal)
        self.assertNotIn("B 근거", rebuttal)

    def test_claude_first_review_counted_in_calls(self):
        ledger = make_ledger([finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")], source="claude-code")
        ledger.reviewer = "claude"
        calls = section(report.render(ledger, False), "## 호출 수와 시간")
        self.assertIn("| Claude 첫 검토 | 1 |", calls)
        self.assertIn("| Claude 의견 | 0 |", calls)

    def test_single_commit_subject_when_body_first_line_empty(self):
        ledger = make_ledger([finding("F1", body="")], [decision("F1", OUTCOME_AGREED_FACT, "a")])
        block = section(report.render(ledger, False), "### 커밋 메시지 초안")
        self.assertIn("fix: 리뷰 지적 F1 반영", block)

    def test_call_counts_and_elapsed(self):
        events = [
            {"step": "start", "at": "2026-10-17T10:00:00+00:00"},
            {"step": "end", "at": "2026-10-17T10:01:05+00:00"},
        ]
        ledger = make_ledger(
            [finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")],
            opinions={"F1": "의견"}, events=events, recheck=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            results = {f"F1:{role}": worker("F1", role) for role in ("a", "b", "writer", "checker")}
            results["F2:a"] = worker("F2", "a")
            (run_dir / "round-1" / "results.json").write_text(json.dumps(results), encoding="utf-8")
            text = report.render(ledger, False, run_dir=run_dir)
        calls = section(text, "## 호출 수와 시간")
        self.assertIn("| a | 2 |", calls)
        self.assertIn("| b | 1 |", calls)
        self.assertIn("| writer | 1 |", calls)
        self.assertIn("| checker | 1 |", calls)
        self.assertIn("| adjudicator | 0 |", calls)
        self.assertIn("| Codex 의견 | 1 |", calls)
        self.assertNotIn("첫 검토", calls)
        self.assertIn("| Codex 재검토 | 1 |", calls)
        self.assertIn("| 1 | 1분 5초 |", calls)

    def test_elapsed_dash_when_fewer_than_two_events(self):
        text = report.render(full_ledger(), False)
        self.assertIn("| 1 | - |", section(text, "## 호출 수와 시간"))

    def test_bucket_decisions_matches_render(self):
        ledger = full_ledger()
        buckets = report.bucket_decisions(ledger)
        self.assertEqual(set(buckets), {"반박", "설계 결정", "판단 불가", "보류 후보"})
        ids = {name: [f["id"] for f, _ in pairs] for name, pairs in buckets.items()}
        self.assertEqual(ids["반박"], ["F3", "F7"])
        self.assertEqual(ids["설계 결정"], ["F4"])
        self.assertEqual(ids["판단 불가"], ["F5"])
        self.assertEqual(ids["보류 후보"], ["F6"])
        text = report.render(ledger, False)
        for name, id_list in ids.items():
            body = section(text, f"### {name}")
            for fid in id_list:
                self.assertIn(f"**{fid}**", body)
        for f, d in buckets["반박"]:
            self.assertEqual(f["id"], d["finding_id"])


class PreparedTest(unittest.TestCase):
    def test_prepared_files_written_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "repo", {"a.txt": "x\n"})
            before_log = git(repo, "log", "--oneline")
            run_dir = repo / ".cascade-run"
            report.write_prepared(run_dir, full_ledger())
            self.assertEqual(git(repo, "log", "--oneline"), before_log)
            staged = git(repo, "diff", "--cached", "--name-only")
            self.assertEqual(staged, "")
            message = (run_dir / "prepared" / "commit-message.txt").read_text(encoding="utf-8")
            self.assertTrue(message.startswith("fix: 2건의 리뷰 지적 반영\n\n- F1 a.py:1: 합의 사실"))
            self.assertNotIn("```", message)
            self.assertEqual(json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8")), [])

    def test_prepared_pr_comment_files(self):
        findings = [finding("F1", thread_id="T1"), finding("F3", thread_id="T3")]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["통과"]),
            decision("F3", OUTCOME_AGREED_NO, None, ["a: 없음", "b: 없음"]),
        ]
        ledger = make_ledger(findings, decisions, source="pr-comment")
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report.write_prepared(run_dir, ledger)
            replies = json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8"))
            resolve = json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8"))
        self.assertEqual(set(replies), {"T1", "T3"})
        self.assertTrue(replies["T1"].startswith("반영했습니다."))
        self.assertTrue(replies["T3"].startswith("확인 결과"))
        self.assertEqual(resolve, ["T1"])

    def test_dry_run_pr_comment_writes_no_replies_or_resolve(self):
        findings = [finding("F1", thread_id="T1"), finding("F3", thread_id="T3")]
        decisions = [
            decision("F1", OUTCOME_AGREED_FACT, "a", ["통과"]),
            decision("F3", OUTCOME_AGREED_NO, None, ["a: 없음", "b: 없음"]),
        ]
        ledger = make_ledger(findings, decisions, source="pr-comment", dry_run=True)
        text = report.render(ledger, True)
        self.assertNotIn("반영했습니다", text)
        self.assertIn("적용 안 됨(dry-run)", section(text, "### PR 답글 초안"))
        self.assertIn("적용 안 됨(dry-run)", section(text, "### resolve 대상"))
        self.assertNotIn("- T1", section(text, "### resolve 대상"))
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report.write_prepared(run_dir, ledger)
            self.assertEqual(json.loads((run_dir / "prepared" / "replies.json").read_text(encoding="utf-8")), {})
            self.assertEqual(json.loads((run_dir / "prepared" / "resolve.json").read_text(encoding="utf-8")), [])


class BaselineAndTimingTest(unittest.TestCase):
    def test_baseline_section_lists_excluded_commands(self):
        ledger = full_ledger()
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "round-1").mkdir()
            (run_dir / "round-1" / "baseline.json").write_text(json.dumps({"failed": ["ruff check ."]}), encoding="utf-8")
            (run_dir / "round-1" / "apply.json").write_text(json.dumps({"applied": [], "failed": [], "baseline_failed": True}),
                                                            encoding="utf-8")
            text = report.render(ledger, False, run_dir)
        body = section(text, "## 기준선 실패")
        self.assertIn("`ruff check .`", body)
        self.assertIn("기준선(수정 전)에서도 실패했다", body)
        self.assertNotIn("## 기준선 실패", report.render(ledger, False))

    def test_report_events_do_not_count_in_elapsed(self):
        events = [{"step": "init", "at": "2026-10-17T10:00:00+09:00"}, {"step": "apply", "at": "2026-10-17T10:00:30+09:00"},
                  {"step": "report", "at": "2026-10-17T12:00:00+09:00", "round": 1}]
        ledger = make_ledger([finding("F1")], [decision("F1", OUTCOME_AGREED_FACT, "a")], events=events)
        self.assertIn("| 1 | 30초 |", report.render(ledger, False))


if __name__ == "__main__":
    unittest.main()
