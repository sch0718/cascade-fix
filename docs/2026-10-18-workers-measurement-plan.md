# 외부 작업자 측정 계획 (2026-10-18)

`--workers external`(Codex·Cursor가 1차 작업자 자리를 맡는 구성)이 `--workers claude`와 비슷한 품질을 내는지 같은 표본으로 재는 절차다. 기준과 근거는 `docs/superpowers/specs/2026-10-18-cursor-codex-workers-design.md` 4.4절이다. 이 계획은 측정 도구(`eval/replay.py --workers`, `eval/score.py --baseline-runs`)를 쓰는 순서만 적는다. 통과하면 `--workers` 기본값을 바꾸는 변경을 따로 연다. 이 문서는 기본값을 바꾸지 않는다.

## 시작 전에 받을 것

실측은 진짜 `codex`·`cursor-agent`를 부른다. 표본의 저장소 코드와 지적이 OpenAI(Codex)와 Cursor(팀 프라이버시 모드)로 나간다. 다음을 사용자에게 받은 뒤에 돌린다.

- [ ] 측정을 시작해도 되는지에 대한 승인
- [ ] 표본 저장소의 코드 일부가 OpenAI와 Cursor로 전송되는 것에 대한 동의(저장소별로 확인한다. 보내면 안 되는 저장소는 표본에서 뺀다)
- [ ] `codex`와 `cursor-agent`의 로그인 상태 확인. 풀려 있으면 사용자가 `codex login`·`cursor-agent login`으로 로그인한다
- [ ] 비용 확인. Cursor `composer-2.5`는 호출당 약 $0.01이다. 표본 30건이면 Cursor 몫 약 30호출이라 $0.3 안팎이고, 재요청과 대체 실행이 더해진다. Cursor의 Auto(grok-4.7-high)는 약 85배라 측정에 쓰지 않는다. Cursor 모델은 `composer-2.5`로 둔다(Claude·GPT 계열 Cursor 모델도 쓰지 않는다)

## 1. 표본 모으기

`eval/sample-format.md`의 형식과 「30건 모으는 방법」을 따른다.

- 코드 모드 출처(`codex-code`·`claude-code`·`cursor-code`·`pr-comment`)에서 30건 이상 모은다.
- `truth.verdict`가 「아님」인 항목을 일부러 섞는다. 「사실」만 모으면 일치율이 과대하게 나온다.
- 커밋 해시는 지적이 나온 시점의 것만 쓴다. 확인하지 못한 지적은 넣지 않는다.
- 문서 모드(`*-doc`) 항목은 넣지 않는다. 회차별 문서 상태가 git에 없어 소급이 안 된다. 문서 모드는 아래 「문서 모드」로 잰다.
- `eval/sample.jsonl`은 이 기기에 없다. 사용자가 과거 실행에서 모으거나 새로 모은다.
- 저장소별 검증 명령은 `repos.toml`에 적어 둔다.

## 2. 두 번 돌리기

같은 표본을 같은 `--repos-toml`로 두 번 돌린다. `--out` 아래 폴더만 다르게 한다. 모든 명령은 스킬 루트에서 돌린다.

```
python3 eval/replay.py --sample eval/sample.jsonl --out <결과>/claude   --workers claude
python3 eval/replay.py --sample eval/sample.jsonl --out <결과>/external --workers external
```

- `--workers claude`는 `jobs`까지 만들고 멈춘다. 항목마다 스킬의 Workflow로 stage1을 돌리고 `judge` → `adjudicate` → `apply` → `report`를 이어간 뒤 `--collect`로 모은다(`eval/sample-format.md`의 「돌리는 순서」).
- `--workers external`은 항목마다 `jobs` 뒤에 `workers --stage stage1`을 스스로 돌려 CLI 작업을 끝낸다. 대체 실행(Claude로 다시 도는 작업)이나 Claude 몫으로 남은 작업이 있을 때만 멈추므로, 그 작업만 Workflow로 돌리고 `judge` 이후를 이어간 뒤 `--collect`로 모은다.
- 두 실행의 Opus 판가름(`claude-opus-5-5` high)은 그대로다. 바뀌는 것은 A·writer와 B·checker 자리의 작업자뿐이다.
- 모델을 부르는 실제 실행 전에 `--workers claude`에서 `--fake-results`로 배관을 먼저 시험할 수 있다. 모델을 부르지 않는다. `--workers external`은 Codex·Cursor를 실제로 부르므로 `--fake-results`와 함께 쓸 수 없고, 함께 주면 오류로 거절한다.
- 한 항목이 오류로 끝나도 나머지는 계속 돈다. 오류 항목은 채점에서 빠지고 표에 따로 센다. 한쪽 실행에서만 빠진 항목이 많으면 두 열의 표본이 달라지므로 「채점한 항목 수」가 같은지 먼저 본다.

## 3. 채점

```
python3 eval/score.py --runs <결과>/external --baseline-runs <결과>/claude --sample eval/sample.jsonl --out <결과>/compare.md
```

표는 「Claude 작업자 | 외부 작업자」 두 열이다. 판정 일치율, Opus 호출 수와 비율, 모델별 Claude 호출 수(Sonnet·Haiku·Opus), 대체 실행(사유별), 무효 결과, CLI별 토큰 사용량, 회귀 건수, 채점한 항목 수가 나란히 나오고 끝에 「비교 판정」 한 줄이 붙는다.

Claude 비용은 호출 수로 갈음한다. Workflow 서브에이전트의 토큰을 스크립트가 읽을 수 없기 때문이다. 외부 작업자 쪽 Claude 호출 수가 Claude 작업자 쪽보다 얼마나 줄었는지(Sonnet·Haiku), 대체 실행으로 얼마나 되돌아왔는지가 비용 차이의 근거다. 메인 세션 토큰은 「다음:」 흐름에서 새로 읽는 출력이 요약 몇 줄뿐인지로 따로 확인한다.

## 4. 통과 기준

설계 4.4절의 기준이고 사용자가 확인한 것이다. 네 가지를 모두 만족해야 「통과」다.

| 기준 | 조건 | 판정 줄의 사유 |
| --- | --- | --- |
| 판정 일치율 | Claude 작업자 결과보다 5%p 넘게 낮지 않다 | `일치율` |
| Opus 호출 수 | Claude 작업자 결과보다 많지 않다 | `Opus 호출` |
| 대체 실행 | 전체 CLI 작업의 10% 이하 | `대체 실행` |
| 회귀 | 0건 | `회귀` |

- 못 미치는 기준은 `미달(일치율, Opus 호출)`처럼 판정 줄에 모두 적힌다.
- 외부 실행에 CLI 작업이 하나도 없으면(CLI 미설치·로그인 만료로 모두 Claude로 돈 경우) 대체 실행을 잴 수 없으므로 `미달(대체 실행 미측정)`이다. 이때는 환경을 고쳐 외부 실행을 다시 돌린다.
- 소급 재생은 항상 dry-run이라 전체 테스트를 돌리지 않는다. 회귀는 「회귀 미측정」으로 나오고 판정에서는 빠진다. 회귀는 외부 작업자로 실제 적용한 전향 실행에서 따로 본다.
- 미달이면 값과 원인(대체 실행 사유, 무효 결과 비율, 항목별 판정 차이)을 보고하고, 구조를 되돌리거나 모델을 바꿀지 사용자가 정한다. 기준을 낮춰 통과시키지 않는다.

## 5. 문서 모드

문서 모드는 소급 표본이 없다. 그래서 이번 소급 측정은 코드 모드만 한다. 문서 모드는 전향으로 비교한다.

- 다음 실제 문서 검토 몇 건에서 같은 입력을 `--dry-run`으로 두 구성(`--workers claude`, `--workers external`) 모두 돌린다.
- 두 구성의 보고서(`report.md`)를 나란히 놓고 판정, 채택 패치, 점검자 결과, 대체 실행 사유를 비교한다. 이 비교는 점수가 아니라 사람이 읽는 것이다.
- 그 전까지 문서 모드의 `external`은 옵션으로만 쓰고 기본값 변경 판단에 넣지 않는다.
- 전송 동의는 문서 검토마다 따로 받는다(문서 내용이 OpenAI와 Cursor로 나간다).

## 6. 끝난 뒤

- `compare.md`와 판정 줄을 사용자에게 보고한다.
- 통과해도 `--workers` 기본값은 이 계획에서 바꾸지 않는다. 바꾸려면 따로 변경을 연다.
- 결과 폴더에는 표본 저장소의 코드와 지적이 들어 있다. 공유 전에 확인하고, 필요 없으면 지운다.
