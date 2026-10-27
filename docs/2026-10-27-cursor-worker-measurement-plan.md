# Cursor 좋은 모델을 작업자 칸에 쓰는 방안 측정 계획 (2026-10-27)

사용자 결정(2026-10-27): 후보는 C1 `grok-4.7-high` 하나, 검증(`claude-`·`gpt-` 금지)은 풀지 않는다. 실행 승인은 아직 없다. 아래 3절 표의 나머지 후보는 참고용이다.

초안이다. 사용자 승인 전에는 아무것도 돌리지 않는다. 2026-10-26 가성비 측정(`docs/2026-10-26-cost-effectiveness.md`)의 수정 단계 표본·방식을 그대로 이어, Cursor의 좋은 모델을 `code.a`·`code.b`에 넣으면 정확도가 오르는지 잰다. 기대하는 것은 Claude 비용 절감보다 정확도다. Cursor 비용은 회사 팀 계정에 잡히므로 실행마다 승인을 받는다.

## 1. 확인한 사실

- 이 기기 `cursor-agent`에서 쓸 수 있는 모델(2026-10-27 `--list-models`): Composer 2.5, Grok 4.7(low~xhigh)·4.6, GPT-5.6 Sol/Luna·5.3 Codex·5.2, Claude Opus 5.5(low~max)·Sonnet 5·Fable 5, Gemini 3.7 Flash.
- **막힌 점**: 프로필 검증이 Cursor 칸에 `claude-`·`gpt-`로 시작하는 모델을 거절한다(`cascade/profiles.py:32,158`). 지금 쓸 수 있는 것은 `composer-*`·`grok-*`·`cursor-grok-*`·`gemini-*`다. Opus 5.5·GPT-5.6 Sol을 Cursor로 쓰려면 이 검사를 풀어야 한다(코드 변경, 사용자 결정).
- 지금까지 Cursor 작업자는 `composer-2.5`뿐이다(`external` 48.6%·오채택 3, `mix-sonnet-cursor` 35.1%·오채택 3, 둘 다 1회). 문서(`docs/reference.md:310`)는 Grok 4.7 high가 composer 대비 호출당 약 85배라 적었다.
- 쓰기 작업자는 호출마다 `cursor-agent -p --sandbox enabled --model <모델>`로 돈다(`cascade/runners.py:_command`). 모델은 프로필 칸의 `model`로 넘어가고 effort는 없다(모델 이름에 들어 있음).
- 측정 도구는 그대로 쓴다: `eval/replay.py --profile`, 채점·`adopt_recalc.py`·`compare.py`. 새 코드는 측정용 프로필 파일뿐이다(결과 폴더 `profiles.toml`, 내장·추천 표 안 바꿈).

## 2. 비교 기준(다시 재지 않고 재사용)

같은 표본 37건(`2026-10-19/sample.jsonl`), 같은 코드 경로(v1.5.1, 이후 #23·#24는 문서·eval뿐), 판가름 Opus high. 재사용 값은 결과 문서의 회차 평균이다.

| 기준 | 정확도 | 「아님」 오채택 | 사람 목록 | 작업 비용 |
| --- | ---: | ---: | ---: | ---: |
| 단일 Claude(3회) | 37.8% | 0 | 21.3 | $0.47 |
| `local`(Codex low + Haiku low, 3회) | 38.7% | 0 | 20.7 | $0.13 |
| `local-sonnet-b`(3회) | 40.5% | 0.3 | 20 | $0.31 |
| `external`(Codex + composer, 1회) | 48.6% | 3 | 15 | $0.21 |
| `mix-sonnet-cursor`(Sonnet + composer, 1회) | 35.1% | 3 | 19 | $0.49 |

날짜가 다른(10-26 대 이번) 모델 서버 변화는 통제하지 못한다. 흔들림이 크므로(단일 Claude 판정 바뀜 19%) 후보당 3회를 권한다.

## 3. 잴 구성(후보, 사용자가 고른다)

한 번에 한 칸만 바꿔 `local`과 비교한다. 기본: `code.a` = Cursor 모델 X, `code.b` = Haiku 5.5 low(`local`과 같음), 판가름 Opus high, 채택 규칙은 끄고 돌린 뒤 켬 값을 다시 계산(지난번과 같음).

| 후보 | 모델 | 막힘 | 비고 |
| --- | --- | --- | --- |
| C1 | `grok-4.7-high` | 없음 | 비용 추정 composer의 85배, 가장 큰 불확실 |
| C2 | `gemini-3.7-flash-high` | 없음 | 싼 편, 다른 계열 |
| C3 | `gpt-5.6-sol-high` | 검증 완화 필요 | Codex와 같은 계열이라 새 정보 적을 수 있음 |
| C4 | `claude-opus-5-5-medium` | 검증 완화 필요 | Claude 계열을 Cursor 계정 비용으로 씀(Claude 한도 절약 가능성) |
| C5 | 양쪽 Cursor(A=Grok, B=Gemini 등) | 없음 | C1·C2 결과를 본 뒤에만 |

권장: 먼저 **확인 실행 1건**으로 후보당 호출 비용·시간을 재고, 그 값으로 남은 회차를 다시 추정해 보고한 뒤 진행한다(지난 측정과 같은 단계적 방식).

## 4. 지표

정확도(맞힘/37), 「아님」 오채택, 사람 목록, 완주(대체 실행·형식 오류 수), Cursor 호출당 토큰·시간, 항목당 처리 시간, Claude 비용(작업 비용, 진행 비용은 지난 추정값 $0.80/지적을 같은 방식으로 더해 별도 표기), 회차 사이 판정 바뀜. 작업자 단계 자체 지표도 본다: 작업자 A가 단독으로 사실·아님을 맞힌 비율.

## 5. 회차·비용·시간 추정

| 단계 | Claude(추정) | Cursor 팀 계정 | 시간 |
| --- | ---: | --- | --- |
| 후보당 확인 실행 1건 | 약 $0.3 | 1호출(실측) | 약 10분 |
| 후보당 1회차 37건 | 약 $8~12(판가름·Haiku·의견) | 37호출 + 재요청, 호출당 값은 확인 실행으로 정함 | 약 1.5~2시간 |
| 후보당 3회 | 약 $25~36 | 111호출 | 약 5~6시간 |

- 후보 2개(C1·C2) × 3회 기준 Claude 약 $50~70, 시간 약 11~12시간(동시 2회 안에서 순차). 후보가 늘면 선형으로 는다. 진행·문서 작성은 약 $10~20.
- Cursor 사용량은 팀 계정 한도에 잡히므로 확인 실행 결과로 달러 단위를 다시 추정해 보고하고, 그 값을 승인받은 뒤 본 실행을 한다. Grok 4.7 high가 문서대로 composer의 85배(호출당 약 $0.85)면 111호출 약 $95 규모로 가장 비싼 후보다.

## 6. 이 기기 제약과 안전장치

- RAM 16GB·여러 세션 동시: 무거운 프로세스는 2개까지(replay 1 + Workflow/판가름 1). 후보 구성끼리 동시에 돌리지 않는다.
- 명령 전체 시간 제한 `perl -e 'alarm shift; exec @ARGV' <초> <명령>`, 항목당 제한은 작업자 러너의 timeout을 쓴다. 메모리는 RSS를 주기적으로 확인해 2GB 넘으면 종료한다.
- 백그라운드 프로세스는 PID를 결과 폴더 `pids.txt`에 적고, 끝나거나 방향을 바꾸면 즉시 종료한다. 턴을 끝내기 전에 남은 프로세스가 없는지 확인한다.
- 한도(Cursor 팀 한도·Claude·Codex 로그인 만료)에 걸리면 어디까지 했는지 `runs.md`에 남기고 멈춘다. 이어서 돌릴 수 있게 항목별 결과를 유지한다.
- 외부 전송: sample-be·synctx 코드를 Cursor(와 그 뒤 모델 제공사)로 보낸다. 실행 전에 저장소별 동의를 새로 받는다(지난 동의는 OpenAI용이었다). 사용 모델이 Cursor 경유 Claude·Grok·Gemini 등인 점도 같이 알린다.
- 쓰기 작업자는 `--sandbox enabled`지만 네트워크·워크트리 밖 쓰기 제한은 지난 측정과 같은 조건이다.
- 결과 폴더 `eval/runs/2026-10-27-cursor-worker/`(git 밖), 끝나면 `~/01.workspace/backup/cascade-fix-eval-runs/`로 옮긴다. 이 기기의 `repos.toml`은 읽지도 고치지도 않는다(`CASCADE_REPOS_TOML`).

## 7. 산출물

- `docs/2026-10-27-cursor-worker-measurement.md`(기존 측정 문서 형식: 한눈에 표, 정답별, 흔들림, 갈린 항목, 비용, 측정 중 생긴 일, 한계), README 요약 갱신.
- 추천 표(`cascade/recommendations.toml`)·내장 프로필은 바꾸지 않는다. 결과를 보여 주고 바꿀지 묻는다.
- 테스트 `python3 -m pytest tests -q` 통과 확인. 커밋은 Conventional Commits, push·PR은 승인 후.

## 8. 한계(예상)

- 표본 37건, 코드 모드, 두 저장소. 1~3건 차이는 흔들림 안이다.
- 정확도 상한이 작업자가 아니라 「설계 결정이면 사람 목록」 규칙에 묶여 있다(정답 「사실」 20건 중 5~7건이 사람 목록으로 간다). 작업자 모델을 올려도 오르는 폭이 작을 수 있다. 오히려 `external` 구성처럼 정확도 상승과 오채택 증가가 같이 오는지가 핵심 관찰점이다.
- 소급 dry-run, 회귀·재검토는 재지 않는다. 단가는 Cursor 계정에서 달러가 아니라 사용량으로 보인다.
