# 측정 표본 형식과 실행 방법

설계 14절의 소급 재생·채점에 쓰는 표본 파일(`eval/sample.jsonl`)의 형식, 모으는 방법, 돌리는 순서를 적는다.

## 한 줄의 형식

한 줄이 지적 하나다. 줄마다 JSON 객체 하나, 빈 줄은 무시한다.

| 키 | 값 |
| --- | --- |
| `id` | 표본 항목 이름. 폴더 이름으로 쓰므로 영문·숫자·`.`·`_`·`-`만 쓴다 |
| `repo` | 지적을 처리한 저장소의 절대 경로 |
| `commit` | 지적이 나온 시점의 커밋. 그 커밋에서 다시 돌린다 |
| `source` | `codex-doc` · `codex-code` · `pr-comment` |
| `doc` | 문서 모드면 문서 경로(저장소 안 절대·상대 경로), 아니면 `null` |
| `finding` | 정규화한 지적(`id`, `source`, `severity`, `location`, `body`, `evidence`, `thread_id`, `outdated`). `id`는 `^[A-Za-z0-9][A-Za-z0-9._-]*$` |
| `truth.verdict` | 그때의 Opus 판정 중 사람이 받아들인 것: `사실` · `아님` · `재현 불가` |
| `truth.accepted_patch` | 사람이 받아들인 수정의 diff. 「아님」이면 `null` |
| `baseline_opus_calls` | 지금 방식에서 Opus가 이 지적에 쓴 호출 수. 지적 1건 = 1 |

예(한 줄로 이어 쓴다. 보기 좋게 줄을 나눴다):

```json
{"id": "be-review-007", "repo": "/Users/me/work/sample-be", "commit": "3f2a9c1d5e7b8a4c6d0e1f2a3b4c5d6e7f8a9b0c",
 "source": "codex-code", "doc": null,
 "finding": {"id": "r1-01", "source": "codex-code", "severity": "Warning", "location": "src/rules/engine.py:88",
             "body": "빈 목록이면 첫 원소를 읽다가 예외가 난다", "evidence": "rules=[] 로 evaluate()를 부르면 IndexError",
             "thread_id": null, "outdated": false},
 "truth": {"verdict": "사실", "accepted_patch": "--- a/src/rules/engine.py\n+++ b/src/rules/engine.py\n@@ -86,3 +86,4 @@\n-    first = rules[0]\n+    if not rules:\n+        return []\n+    first = rules[0]\n"},
 "baseline_opus_calls": 1}
```

`finding.id`는 표본 안에서 겹쳐도 된다. 채점은 항목 `id`로 찾고 항목 안에서 `finding.id`의 마지막 판정을 본다.

## 30건 모으는 방법

목표는 30건 이상이고 「사실」과 「아님」이 섞여야 한다. 「아님」은 일부러 넣는다. 일치율이 「사실」만 모은 표본에서는 과대하게 나온다.

- [ ] 출처 1: `sample-be`의 Codex 스킬 리뷰 세션. 세션 기록에서 Codex가 낸 지적과 그때 Opus가 내린 판정(사실·아님·재현 불가)을 뽑는다.
- [ ] 출처 2: `sample-docs`의 지침 검토 1~9차 agentmemory 기록. 지적별 판정이 적혀 있고 커밋이 남은 것만 쓴다.
- [ ] 커밋 해시는 지적이 나온 시점의 것이다. `git log`로 그 검토 직전 커밋을 확인한다. 해시를 확인하지 못한 지적은 넣지 않는다.
- [ ] `truth.verdict`는 그때 Opus 판정이 아니라 사람이 최종 받아들인 판정으로 적는다. 둘이 다르면 사람 쪽이다.
- [ ] 사실 항목의 `accepted_patch`는 실제로 반영된 커밋의 diff에서 가져온다.
- [ ] `baseline_opus_calls`는 지적 1건당 1로 센다. 같은 지적을 여러 번 다시 본 기록이 있으면 그 횟수를 적는다.
- [ ] 문서 모드(`codex-doc`) 항목은 넣지 않는다. 회차별 문서 상태가 git에 없어 소급이 안 된다. 문서는 전향(실제 검토를 dry-run으로 같이 돌림)으로만 잰다. 전향 항목은 `doc`에 그 시점 문서를 두고 같은 형식으로 쓸 수 있다.
- [ ] PR 코멘트(`pr-comment`)는 `judge-eval/review-labels.jsonl`의 사람이 남긴 14건을 입력으로 쓴다. 정답 수정이 없으므로 `truth.accepted_patch`는 `null`이고 수정은 사람이 따로 채점한다.
- [ ] 저장소별 검증 명령은 `repos.toml`에 적어 둔다. `replay.py`가 워크트리 경로에 맞춰 설정을 복사해 쓰므로 원래 저장소 경로로 적으면 된다.

## 돌리는 순서

모든 명령은 스킬 루트(`~/.claude/skills/cascade-fix`)에서 돌린다.

1. 소급 재생. 항목마다 그 커밋의 워크트리를 만들고 `init --dry-run`부터 `report`까지 돌려 원장을 모은다.

   ```
   python3 eval/replay.py --sample eval/sample.jsonl --out <결과 폴더> [--limit N] [--repos-toml F]
   ```

   모델을 실제로 부르는 단계는 스킬의 Workflow가 한다. 위 명령은 `jobs`까지 만들고 멈추며 `meta.json`에 `pending_run_dir`을 적는다. 항목마다 Workflow로 `stage1.json`(필요하면 `adj.json`)을 돌리고 `judge` → `adjudicate` → `apply` → `report`를 이어간 뒤 다음으로 모은다.

   ```
   python3 eval/replay.py --sample eval/sample.jsonl --out <결과 폴더> --collect
   ```

   `--collect`는 `cleanup`을 돌리고 `run_dir`을 `<결과 폴더>/<id>/run/`으로 복사하며 `ledger.json`을 `<결과 폴더>/<id>/`에 놓고 워크트리를 지운다.

2. 모델 없이 배관만 시험하려면 `--fake-results <폴더>`를 준다. 폴더 안에 `<id>/stage1.json`(필요하면 `adj.json`, `checker.json`)을 두면 `judge`·`adjudicate`·`apply`·`report`·`cleanup`까지 한 번에 돌고 항목마다 워크트리를 지운다.

3. 채점.

   ```
   python3 eval/score.py --runs <결과 폴더> --sample eval/sample.jsonl [--out score.md]
   ```

   마크다운 표 하나와 끝의 성공 기준 줄이 나온다. 표의 값은 설계 14절의 것이다.

   | 값 | 계산 |
   | --- | --- |
   | 판정 일치율 | 예측 판정이 `truth.verdict`와 같은 항목 ÷ 판정이 나온 항목. 합의-사실·판가름(패치 채택)은 사실, 합의-아님·판가름(아님)은 아님, 이유에 「재현 불가」가 있으면 재현 불가. 판단불가·설계결정·보류후보는 판정 없음이라 분모에서 뺀다 |
   | 채택 경로 분포 | 합의(합의-사실·합의-아님) / 판가름 / Codex 검토(판단불가·설계결정·보류후보 중 Codex 의견이 원장에 붙은 것) / 사람(나머지) |
   | 패치 다르지만 둘 다 통과 | 판정 이유에 「패치가 다름」이 있는 항목 ÷ 채점한 항목 |
   | Opus 호출 수 | 회차별 `results.json`의 `<id>:adjudicator` 개수 |
   | 기준 Opus 호출 수 | `baseline_opus_calls`의 합. 비율은 호출 수 ÷ 기준 |
   | 회귀 건수 | `apply.json`의 `final_ok`가 `false`인 항목 |
   | 문서 점검자 정확도 | 문서 항목에서 점검자가 「아니오」라 한 것 중 판가름이 「사실」이라 한 비율. 없으면 `-` |
   | 오류 항목 수 | `replay.py`가 `meta.json`에 오류를 적은 항목. 채점에서 뺀다 |

   성공 기준은 판정 일치 95% 이상, Opus 호출 40% 이하, 회귀 0건이다. 하나라도 못 미치면 값과 원인을 보고하고 구조를 되돌릴지 사용자가 정한다.
