# 가성비 측정 계획: cascade-fix vs 단일 Claude (2026-10-26)

cascade-fix를 쓰는 것이 Claude 하나로 직접 고치는 것(단일 Claude)보다 가성비가 좋은지 같은 표본으로 잰다. 결과는 `docs/2026-10-26-cost-effectiveness.md`에 있다. 아래 4절의 진행 비용 예비 결과($0.72)는 포크로 겹친 세션을 거르기 전 값이고, 결과 문서의 값($0.80)이 최종이다. 지금까지 측정(`docs/2026-10-23-role-profiles-measurement-stage1.md`, `docs/2026-10-24-role-profiles-measurement-stage2.md`)은 cascade-fix 구성끼리만 비교했다.

사용자 결정(2026-10-26): 단일 Claude는 `claude -p` 헤드리스, Opus 5.5 effort high. 회차는 단계적(6절 순서). 차트는 라이트·다크 SVG 산점도. 유료 실행은 실행마다 승인과 저장소별 전송 동의를 받는다. 진행 비용은 평균 규모 지적당 값을 대표로 넣고 민감도를 함께 보인다.

- 확인 실행 승인(2026-10-26): 단일 Claude 1건, `m-local-sonnet-b` 1건.
- 전송 동의(2026-10-26): sample-be·synctx 모두 OpenAI(Codex) 동의. `m-local-sonnet-b` 확인 실행과 3회차까지의 본 실행에 적용한다.
- 확인 실행 결과(2026-10-26, 첫 항목 rsbe-pr758-t5, 정답 「아님」): 단일 Claude는 「사실·설계 결정」으로 사람 목록, 32초·7턴·$0.45(세션 기록 집계가 `claude -p`의 `total_cost_usd`와 같음). 사용자 플러그인·훅·MCP는 실리지 않았고 `~/.claude/CLAUDE.md`는 실렸다(평소 조건과 같아 그대로 둠). `m-local-sonnet-b`는 배정이 계획과 같고 `substituted` 없음·채택 규칙 끔, Codex 작업자 성공(입력 21만 토큰), Sonnet B $0.24, 판정 설계결정.
- 1회차 승인(2026-10-26): 두 구성 37건씩, 단일 Claude 동시 1과 replay를 겹쳐 돌린다.

## 1. 지난 결과 재사용 판단

지난 결과는 `34d162b`(v1.4.0)에서 쟀다. `git diff 34d162b..55e33e1`로 코드 모드 경로를 확인했다.

| 바뀐 곳 | 코드 모드 소급 재생에 영향 |
| --- | --- |
| `prompts/*`, `scripts/run-jobs.js`, `cascade/consensus.py`·`patches.py`·`verify.py`·`jobs.py`·`workers.py`·`assignment.py`·`profiles.py`·`usage.py`·`snapshot.py` | 바뀌지 않음 |
| `cascade/runners.py` Codex | `_run`을 `_exec`·`_failed`로 나눈 구조 변경과 `ask` 추가. 명령줄 인자·결과 처리 같음 |
| `cascade/runners.py` Cursor | 읽기 역할(ACP)에 워크트리 밖 Read 거부 추가. 코드 작업자(쓰기, `-p`)는 그대로 |
| `cascade/cli.py` | `CF setup` 추가, `detect`에 안내 줄 추가. `init`·`jobs`·`judge`·`adjudicate`·`apply`·`report` 그대로 |
| `cascade/cli_steps.py`·`config.py` | `has_repos` 이동(동작 같음) |
| `SKILL.md` | 설정 절 추가. 「Cursor 실행 방식」 설명 갱신(읽기 역할만) |

→ 코드 모드 작업자·판가름·의견 경로가 같으므로 지난 결과를 그대로 쓴다. 다시 재지 않는다.

## 2. 비교할 구성

| 이름 | code.a | code.b | 판가름 | 회차 | 출처 |
| --- | --- | --- | --- | ---: | --- |
| **단일 Claude**(기준선) | Opus 5.5 혼자 판정·수정·테스트 | - | - | 3(새로) | 이번 |
| `claude-only` | Sonnet low | Haiku low | Opus high | 3 | 1·2단계 |
| `m-mix-codex-haiku`(= 내장 `local`) | Codex low | Haiku low | Opus high | 3 | 1·2단계 |
| `m-external` | Codex low | Cursor composer-2.5 | Opus high | 1 | 1단계 |
| `m-mix-sonnet-cursor` | Sonnet low | Cursor composer-2.5 | Opus high | 1 | 1단계 |
| `m-local-sonnet-b`(이 기기 설정) | Codex medium | Sonnet medium | Opus high | 3(새로) | 이번 |

- 채택 규칙은 지난번처럼 끄고 돌린 뒤 켬 값을 반사실로 다시 계산한다(`adopt_recalc.py`). 내장 프로필 기본값이 켬이므로 표의 대표값은 켬이다.
- `m-local-sonnet-b`는 측정용 `profiles.toml`에 아래로 만든다. 이 기기의 `~/.claude/cascade-fix/repos.toml`은 읽지도 고치지도 않는다(`CASCADE_REPOS_TOML`로 결과 폴더의 파일만 쓴다).

```toml
[profiles.m-local-sonnet-b]
base = "local"
adopt_on_both_pass = false
"code.a" = { runner = "codex", effort = "medium" }
"code.b" = { runner = "claude", model = "claude-sonnet-5-5", effort = "medium", fallback = { runner = "claude", model = "claude-sonnet-5-5", effort = "medium" } }
```

  확인 실행에서 `역할:` 줄과 원장 `profile.roles`가 이 기기의 `CF detect --profile local-sonnet-b` 결과(코드 칸)와 같은지 대조한다.

## 3. 단일 Claude 모드 (`eval/solo.py`, 새 스크립트)

replay에 넣지 않고 따로 만든다. replay는 cascade-fix 명령(init·jobs…)을 잇는 도구라 흐름이 다르다. 공통 부분(표본 읽기, 워크트리 만들기·지우기, 워크트리용 repos.toml)은 `eval/replay.py` 함수를 불러 쓴다.

### 실행

- 항목마다 `replay.py`와 같은 방식으로 표본 커밋에 분리 워크트리를 만든다(훅 끔). 저장소의 `setup` 명령(예: `.venv` 링크)도 cascade-fix 작업자와 같게 돌린다.
- 항목마다 Claude Code 헤드리스 세션 하나: `claude -p --model claude-opus-5-5 --effort <effort> --output-format json`을 워크트리에서 띄운다.
  - 사용자 설정·플러그인·훅·MCP를 싣지 않는다(`--setting-sources project`, `--strict-mcp-config`). agentmemory 저장·superpowers 주입 같은 부작용을 막고, cascade-fix 작업자와 조건을 맞춘다. `~/.claude/CLAUDE.md`가 실리는지는 확인 실행의 세션 기록으로 본다.
  - 편집·Bash는 자동 승인한다(cascade-fix 서브에이전트도 세션 권한으로 돈다). 네트워크도 막지 않는다(지난 판가름과 같은 조건).
  - 하위 에이전트를 써도 막지 않고, 쓰면 그 토큰도 센다.
  - 동시에 2개까지(사용자 규칙). 항목당 시간 제한 30분.
- 프롬프트(`eval/prompts/solo-code.md`, 새로): `prompts/worker-code.md`에서 「두 작업자」 문장만 뺀 꼴이다. 「설계 결정 기준」 절은 실행 때 `worker-code.md`에서 그대로 읽어 끼운다(문구가 갈라지지 않게). 지적 본문·위치·등급, 워크트리 경로, 저장소 검증 명령(tests·lint·typecheck·format)을 준다. 정답·다른 구성 결과·cascade-fix 흐름은 주지 않는다. 출력은 작업자와 같은 JSON(`verdict`·`needs_design_decision`·`reason`·`verify`).
- 끝나면 워크트리 변경(추적 안 된 파일 포함)을 패치로 뽑고, 검증 명령을 그 패치에 한 번 더 돌려 통과 여부를 기록한다. 그 뒤 워크트리를 지운다.

### 판정 매핑(cascade-fix와 같은 뜻)

| 단일 Claude 출력 | 채점 판정 |
| --- | --- |
| 사실 · 설계 결정 아님 · 패치 있음 | 사실 |
| 사실 · 설계 결정 | 판정 없음(사람 목록) |
| 아님 | 아님 |
| 재현 불가 | 재현 불가 |
| 못 정함 · 출력 형식 오류 | 판정 없음 |
| 시간 초과 · 실행 오류 | 맞히지 못함(오류) |

- 「사실」인데 다시 돌린 검증이 실패하면 판정은 그대로 「사실」로 세고 「검증 실패 채택」으로 따로 센다. cascade-fix는 검증 실패 패치를 채택하지 않으므로 이 수를 표에 함께 보인다.
- 「사실」인데 패치가 비었으면 판정 없음으로 센다(cascade-fix의 빈 패치 처리와 같다).

### 채점

`eval/solo.py score`가 `score.py --json`과 같은 키(정확도·「아님」 오채택·사람 목록·완주·정답별 표·Claude 비용)를 낸다. 비교 스크립트가 두 형식을 같이 읽는다. 정확도 정의(맞힌 수 / 표본 전체)는 같다.

### 테스트(먼저 쓴다)

`tests/test_solo.py`: 판정 매핑 6갈래, 프롬프트에 설계 결정 기준이 그대로 들어감·정답이 들어가지 않음, 패치 추출(추적 안 된 파일 포함), 시간 초과·형식 오류 처리, 점수 JSON 키. `claude`는 가짜 실행 파일(`tests/fake_cli.py` 방식)로 대신한다.

## 4. 비용 집계 보강

지금 `CF usage`는 Workflow 하위 에이전트만 센다. 비교를 공정하게 하려고 비용을 두 층으로 나눈다.

| 층 | cascade-fix | 단일 Claude |
| --- | --- | --- |
| 작업 비용 | 작업자·점검자·판가름·의견 에이전트(지금 `claude-usage.json`) | 항목 세션 전체(하위 에이전트 포함) |
| 진행 비용 | 메인 세션이 명령을 잇고 Workflow를 띄우는 몫(**추정**) | 없음(세션 자체가 작업) |

- **같은 방식으로 센다**: 세션 기록 JSONL의 `message.usage`를 메시지 id마다 마지막 값으로 세고(지금 `cascade/usage.py`의 방식), 같은 단가표(`PRICES`, 2026-10-06 기준)로 값을 매긴다. `cascade/usage.py`에 세션 파일 하나를 읽는 함수(`read_session`)를 더해 Workflow 읽기와 같은 코드를 쓴다. 단일 Claude는 `claude -p`가 내는 `total_cost_usd`와 대조해 검산한다.
- **진행 비용 추정**(`eval/orchestration_cost.py`, 새로): 이 기기 세션 기록 중 cascade-fix를 실제로 돌린 메인 세션(측정 세션 제외, init이 있는 약 30개 세션에서 고름)에서, 첫 `CF detect`/`init`부터 `report`까지 메인 세션 메시지의 토큰만 센다(하위 에이전트 제외). 실행마다 (지적 수, 진행 비용)을 모아 「실행당 고정 + 지적당」으로 맞춘다. 표본 항목 하나는 지적 1건짜리 실행이므로 이 값을 구성마다 항목당 더한다. 표에는 작업 비용과 진행 비용을 나눠 적고, 진행 비용은 「추정」으로 표시한다.
  - 측정 때의 메인 세션 비용은 37건을 한꺼번에 묶어 돌려 실제 사용과 달라 쓰지 않는다.
  - 실제 실행의 지적 수가 보통 여러 건이라, 지적 1건짜리 항목에 고정 몫을 통째로 붙이면 cascade-fix에 불리하다. 그래서 「지적 1건 실행」과 「실제 평균 실행 규모(지적 n건)에서 지적당」 두 값을 같이 보인다.
- **예비 결과(2026-10-26, 기록만 읽음)**: 실제 사용 실행 7회(sample-be·infra·synctx·sample-docs, 포크로 겹친 세션 2개는 한 번만 셈)에서 진행 비용은 실행당 $1.21~4.68, 맞춤 「고정 $0.64 + 지적당 $0.53」, 평균 규모(3.3건)에서 지적당 $0.72였다. 모두 Opus 5.5 메인 세션이고 85% 이상이 캐시 읽기(실행당 요청 60~110회 × 쌓인 대화 맥락)다. cascade-fix 작업 비용(`m-mix-codex-haiku` 켬, 항목당 약 $0.13)보다 커서 비교 결과를 좌우한다.
  - 맥락 크기에 좌우되므로 공정성 주의: 헤드리스 단일 Claude는 빈 맥락에서 시작하지만, 실제로 사용자가 메인 세션에서 직접 고치면 그 세션의 쌓인 맥락을 같이 읽는다. 그래서 대표값(진행 비용 포함) 옆에 진행 비용을 뺀 값과 지적 1건 실행 값($1.17)을 민감도로 함께 보인다.
  - 메인 세션의 자동 모드 분류기 호출 비용은 세션 기록에 남지 않아 세지 못한다.
- **외부 사용량**: Codex·Cursor 토큰은 지금처럼 원장의 `workers` 이벤트에서 센다. 달러로 바꾸지 않는다(구독 한도가 따로다). Cursor는 호출당 약 $0.01로 참고만 적는다.
- **시간**: 항목당 = 그 항목의 에이전트·CLI 실행 시간 합(JSONL 타임스탬프, `workers` 이벤트), 전체 = 실행 한 번의 벽시계 시간(동시 실행 수를 함께 적음). 지난 결과는 Workflow 기록(85개 남아 있음)과 로그에서 다시 뽑는다.

## 5. 지표

| 지표 | 정의 |
| --- | --- |
| 정확도 | 판정이 정답과 같은 항목 / 37. 사람 목록·오류는 맞히지 못함 |
| 「아님」 오채택 | 정답 「아님」을 「사실」로 채택한 수 |
| 사람 목록 | 판정 없음(설계 결정·판단 불가) 수 |
| 검증 실패 채택 | 단일 Claude만. 「사실」인데 검증 명령이 실패한 수 |
| Claude 비용 | 작업 비용(켬) + 진행 비용(추정), 달러 추정. 이용 한도는 단일 Claude 대비 비율(%)로 같이 적는다(한도가 비용에 비례한다고 가정) |
| Codex·Cursor 사용량 | 입력·출력·캐시 읽기 토큰 |
| 시간 | 항목당 에이전트 시간 합, 실행 한 번 벽시계 |
| 흔들림 | 3회 안에서 판정이 바뀐 항목 비율, 지표마다 최소~최대 |

## 6. 실행 계획과 비용 추정

| 실행 | Claude 비용(추정) | 외부 | 시간(추정) |
| --- | ---: | --- | --- |
| 단일 Claude 확인 1건 | 약 $1 | - | 약 10분 |
| 단일 Claude 3회 × 37건 | 약 $80~165(회당 $26~55) | - | 회당 약 2~2.5시간(동시 2) |
| `m-local-sonnet-b` 확인 1건 | 약 $0.5 | Codex 1작업 | 약 10분 |
| `m-local-sonnet-b` 3회 × 37건 | 약 $45~65(회당 $15~21, 끔 기준·의견 포함) | Codex 111작업(입력 약 2,000만 토큰) | 회당 약 1.5시간 |
| 진행 비용 추정·지난 결과 시간 추출 | 0(기록 읽기) | - | - |
| 이 세션의 진행(Workflow 운영, 문서) | 약 $20~40 | - | - |
| **합계** | **약 $150~270** | | 약 10~12시간 |

- 근거: 단일 Claude는 지난 판가름(Opus high, 호출당 $0.38~0.65)이 재현만 하고도 이 정도였으므로, 수정·테스트까지 하면 1.5~2.5배로 봤다. `m-local-sonnet-b`는 지난 `m-claude-only`(Sonnet low $6.4~7.2/회)의 Sonnet 몫을 medium으로 올리고 Haiku 몫을 Codex로 바꾼 값에 판가름 $7~11을 더했다.
- 순서(권장, 단계적): ① 두 구성 확인 실행 → ② 단일 Claude 1회 + `m-local-sonnet-b` 1회 → 중간 보고(실제 비용으로 남은 회차 추정 갱신) → ③ 각 2·3회차. 무거운 프로세스는 동시 2개까지라, 단일 Claude(동시 2)와 replay를 겹치지 않는다. 단일 Claude를 동시 1로 낮추면 replay와 겹쳐 돌 수 있다.
- 외부 전송: `m-local-sonnet-b`의 code.a·code.opinion이 sample-be·synctx 코드를 OpenAI(Codex)로 보낸다. 실행 전에 저장소별 동의를 받는다. Cursor는 새로 돌리지 않는다. 단일 Claude는 Anthropic만 쓴다.
- 로그인: `codex` ChatGPT 로그인 확인함(2026-10-26). `cursor-agent`는 이번에 쓰지 않는다.
- 결과 폴더: `eval/runs/2026-10-26-cost/`(git 밖). 끝나면 `~/01.workspace/backup/cascade-fix-eval-runs/`로 옮긴다. 표본은 `2026-10-19/sample.jsonl`을 복사해 쓴다.

## 7. 산출물

- `eval/solo.py`, `eval/prompts/solo-code.md`, `eval/orchestration_cost.py`, `eval/compare.py`(점수 JSON들 → 요약 표 Markdown과 차트 SVG), `cascade/usage.py` `read_session`. 모두 테스트를 먼저 쓴다.
- `docs/2026-10-26-cost-effectiveness.md`: 측정 환경, 구성, 결과 표(끔·켬), 정답별 표, 비용(작업·진행 분리, 칸별), 외부 사용량, 시간, 흔들림, 측정 중 생긴 일, 한계.
- README: 한눈에 보는 요약 표 하나와 차트 하나. 차트는 「정확도(세로, %) 대비 항목당 Claude 비용(가로, $)」 산점도. 점마다 구성 이름 레이블, 3회 구성은 최소~최대 막대, 모양으로 구분(원·사각·삼각, 색만으로 구분하지 않음), 적-녹 조합 없음, WCAG AA 대비. 생성은 `eval/compare.py`.
- 버전: 추천 표를 바꾸지 않으면 문서·eval만 바뀐다. 그때 버전을 올릴지는 끝에서 묻는다.

## 8. 한계(결과 문서에 적을 것)

- 표본 37건, 코드 모드만, 두 저장소(sample-be 31·synctx 6).
- 소급 dry-run이다. 회귀(전체 테스트·사람 되돌림)는 재지 못한다. 표본 커밋 뒤의 정보(같은 저장소 뒤 커밋, 외부 저장소)를 에이전트가 쓸 수 있다(모든 구성 같은 조건).
- 지난 결과(10-22~24)와 이번(10-26 이후)은 날짜가 다르다. 코드 경로는 같지만 모델 서버 쪽 변화는 통제하지 못한다.
- 진행 비용은 실제 사용 기록에서 추정한 값이다.
- 비용 단가 기준일 2026-10-06. 구독 이용 한도가 이 비용에 비례한다고 가정한다.
- `m-external`·`m-mix-sonnet-cursor`는 1회라 흔들림을 모른다.
- 패치 내용이 정답 패치와 같은지는 어느 구성도 자동 채점하지 않는다(판정만 채점). 단일 Claude는 검증 통과 여부를 따로 보인다.
