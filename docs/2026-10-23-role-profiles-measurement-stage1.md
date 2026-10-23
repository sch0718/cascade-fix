# 역할 프로필 측정 1단계 결과 (2026-10-23)

`docs/2026-10-21-role-profiles-measurement-plan.md` 4절 1단계(코드 모드 소급 재생 1회)를 네 구성으로 돌렸다. 기준선은 `m-claude-only`이고, 채택 규칙은 끄고 돌린 뒤 켬 값을 반사실로 다시 계산했다. 2단계(반복)는 하지 않았다.

**결과: 거름 조건을 넘은 구성은 기준선과 `m-mix-codex-haiku` 둘이다. 끔·켬 모두 `m-mix-codex-haiku`가 선정됐다(Claude 비용 켬 $3.96, 기준선 $10.54의 38%). `m-external`과 `m-mix-sonnet-cursor`는 「아님」 오채택(3건, 기준선 1건)으로 걸렀다.**

## 측정 환경

- 코드: main `34d162b`(v1.4.0). 계획서 1절의 전제 수정은 모두 들어 있다.
  - 빈 검증 통과: `1b898ad`
  - 코드 작업자 설계 결정 기준: `5196ca5`. 계획서가 적은 `98794cc`와 patch-id가 같다(같은 변경이 다시 적용된 커밋). 그래서 `merge-base --is-ancestor 98794cc`는 실패하지만 내용은 들어 있다.
  - 문서 판정자 설계 결정 기준: `8228159`
- 표본: `~/01.workspace/backup/cascade-fix-eval-runs/2026-10-19/sample.jsonl`을 결과 폴더로 복사해 썼다. 코드 모드 37건(사실 20 · 아님 16 · 재현 불가 1), sample-be 31건과 synctx 6건이다. 백업은 고치지 않았다.
- 결과 폴더: `eval/runs/2026-10-22-stage1/`(git 제외). 측정용 `profiles.toml`은 백업의 `repos.toml`의 `[repos.*]`에 계획서의 `[profiles.m-*]` 넷을 더한 것이다. 사용자 `repos.toml`의 `[profiles.*]`는 복사하지 않았다(원래 없다).
- 외부 전송 동의: sample-be·synctx 모두 OpenAI(Codex)·Cursor(팀 프라이버시 모드)에 동의했다(2026-10-22). 빠진 항목은 없다.
- 로그인: `codex` ChatGPT 로그인, `cursor-agent` 팀 로그인. 측정 중 만료되지 않았다.
- Codex 모델은 `~/.codex/config.toml`의 `gpt-6.1-sol`(작업자 칸 effort low), Cursor는 `composer-2.5`다.

| 구성 | code.a | code.b | 판가름 | 의견(판단 불가 시) |
| --- | --- | --- | --- | --- |
| `m-claude-only`(기준선) | Sonnet 5.5 low | Haiku 5.5 low | Opus 5.5 high | Fable 5.1 high |
| `m-mix-codex-haiku`(base `local`) | Codex low | Haiku 5.5 low | Opus 5.5 high | Codex |
| `m-external` | Codex low | Cursor composer-2.5 | Opus 5.5 high | Fable 5.1 high |
| `m-mix-sonnet-cursor` | Sonnet 5.5 low | Cursor composer-2.5 | Opus 5.5 high | Fable 5.1 high |

## 잰 방법

계획서 4절 1단계 절차를 그대로 따랐다.

- 구성마다 확인 실행(`--limit 1`, `check/<구성>/`)을 `--collect`까지 끝내고, `프로필: <구성> (--profile)`, `덮음:` 줄 없음, 원장 `adopt_on_both_pass=false`, 확정 배정(`profile.roles`)이 위 표와 같음, `profile.substituted` 비어 있음을 확인한 뒤 본 실행을 했다. 네 구성 모두 맞았다. 확인 실행은 채점에 넣지 않았다.
- 본 실행 뒤에도 37건 모두 확정 배정이 표와 같고 `substituted`·`runtime_substitutions`가 비어 있었다.
- Claude 작업은 스킬의 Workflow로 돌리고, Workflow마다 항목별로 `CF usage --run-dir … --round 1 --workflow <id>`를 돌렸다. 이름표가 작업과 맞지 않은 에이전트는 없었다(다른 항목의 에이전트는 항목마다 「일치하지 않음」으로 빠진다. 아래 「측정 중 생긴 일」).
- `judge` → `adjudicate` → 의견 → `apply` → `report`까지 이었다. 재검토는 돌리지 않았다. `--collect` 전에 37건 모두 `round-1/apply.json`·`report.md`가 있고, `CF status`의 「다음:」이 `cleanup` 또는 재검토 Workflow뿐임을 확인했다.
- 채점: 구성마다 `eval/adopt_recalc.py`로 켬 폴더를 만들었다(검증 셋 모두 통과). `eval/score.py --round 1 --cost-scope round1-core`를 끔은 끔끼리(`--baseline-runs m-claude-only`), 켬은 켬끼리(`--baseline-runs m-claude-only/adopt-recalc`) 냈다. 선정은 `eval/select_profiles.py`를 끔·켬 따로 한 번씩 돌렸다.
- 오류 항목은 없었고 다시 돌린 항목도 없다.

## 결과

### 끔(실행 그대로)

| 값 | m-claude-only | m-mix-codex-haiku | m-external | m-mix-sonnet-cursor |
| --- | ---: | ---: | ---: | ---: |
| 정확도(맞힘/37) | 14 (37.8%) | 14 (37.8%) | 18 (48.6%) | 13 (35.1%) |
| 「아님」 오채택 | 1 | 0 | 3 | 3 |
| 사람 목록 | 21 | 21 | 15 | 19 |
| 완주(오류·미완료·누락) | 0/37 | 0/37 | 0/37 | 0/37 |
| 대체 실행 | 해당 없음 | 0/37 | 0/74 | 0/37 |
| Opus 호출 | 18 | 17 | 27 | 26 |
| Claude 호출 (Sonnet · Haiku · Opus) | 37 · 37 · 18 | 0 · 37 · 17 | 0 · 0 · 27 | 37 · 0 · 26 |
| Claude 비용(round1-core) | $13.67 | $7.06 | $10.93 | $16.79 |
| 선정 조건 | 통과 | 통과 | 미달(「아님」 오채택) | 미달(「아님」 오채택) |

### 켬(채택 규칙 반사실)

| 값 | m-claude-only | m-mix-codex-haiku | m-external | m-mix-sonnet-cursor |
| --- | ---: | ---: | ---: | ---: |
| 정확도(맞힘/37) | 14 (37.8%) | 14 (37.8%) | 18 (48.6%) | 13 (35.1%) |
| 「아님」 오채택 | 1 | 0 | 3 | 3 |
| 사람 목록 | 21 | 21 | 15 | 19 |
| 규칙으로 빠진 판가름 | 8 | 8 | 13 | 12 |
| Opus 호출 | 10 | 9 | 14 | 14 |
| Claude 비용(round1-core) | $10.54 | $3.96 | $5.51 | $11.84 |
| 선정 조건 | 통과 | 통과 | 미달(「아님」 오채택) | 미달(「아님」 오채택) |

- 반사실 검증(`adopt_recalc.py`의 `check.json`)은 네 구성 모두 통과했다. 끈 재계산의 분기가 원장과 37건 모두 같고, 바뀐 항목 수와 빠진 판가름 수가 같고, 뺀 Opus 토큰이 그 항목들의 작업별 판가름 토큰 합과 같다.
- 켬에서 판정이 바뀐 항목은 네 구성 모두 없다. 규칙이 채택한 항목은 모두 Opus도 「사실」로 굳혔던 항목이다. 그래서 정확도·「아님」 오채택·사람 목록이 끔과 같고 비용만 준다.

### 거름 사유와 선정

`select_profiles.py` 출력(끔·켬 같음):

- `m-claude-only`: 통과 — 대체 실행 해당 없음 · 회귀 미측정
- `m-mix-codex-haiku`: 통과 — 회귀 미측정
- `m-external`: 거름 — 「아님」 오채택 미달(3건, 기준선 1건 이하) · 회귀 미측정
- `m-mix-sonnet-cursor`: 거름 — 「아님」 오채택 미달(3건, 기준선 1건 이하) · 회귀 미측정
- 선정: **`m-mix-codex-haiku`** — 끔 Claude 비용 $7.06, 켬 $3.96. 남은 두 구성의 비용 차이가 10%를 크게 넘어(켬 기준 62% 적음) 사람 목록 수로 가르지 않았다(둘 다 21).

1회 측정이라 「불안정」 표시는 판단할 수 없다(2단계에서 본다). 회귀는 소급 재생이 dry-run이라 재지 못했다(4단계).

### 정답별

| 정답 | m-claude-only | m-mix-codex-haiku | m-external | m-mix-sonnet-cursor |
| --- | --- | --- | --- | --- |
| 사실(20) 맞힘/판정/사람 | 14/14/6 | 14/15/5 | 17/17/3 | 13/14/6 |
| 아님(16) 맞힘/판정/사람 | 0/1/15 | 0/2/16 | 1/4/12 | 0/3/13 |
| 재현 불가(1) 맞힘/판정 | 0/1 | 0/1 | 0/1 | 0/1 |

### 구성끼리 판정이 갈린 항목

| 항목 | 정답 | m-claude-only | m-mix-codex-haiku | m-external | m-mix-sonnet-cursor |
| --- | --- | --- | --- | --- | --- |
| rsbe-pr676-c0 | 아님 | **사실** | 판정 없음 | **사실** | **사실** |
| rsbe-pr723-t6 | 아님 | 판정 없음 | 판정 없음 | 판정 없음 | **사실** |
| rsbe-pr723-w1 | 아님 | 판정 없음 | 판정 없음 | **사실** | 판정 없음 |
| rsbe-pr729-t3 | 아님 | 판정 없음 | 판정 없음 | **사실** | **사실** |
| rsbe-pr726-c0 | 아님 | 판정 없음 | 재현 불가 | 아님 | 판정 없음 |
| rsbe-pr729-w5 | 아님 | 판정 없음 | 재현 불가 | 판정 없음 | 판정 없음 |
| rsbe-pr720-c1 | 사실 | 판정 없음 | 아님 | 사실 | 아님 |
| rsbe-pr752-t0 | 사실 | 판정 없음 | 사실 | 사실 | 판정 없음 |
| rsbe-pr752-t1 | 사실 | 판정 없음 | 판정 없음 | 사실 | 판정 없음 |
| rsbe-pr758-t10 | 사실 | 판정 없음 | 판정 없음 | 사실 | 사실 |
| synctx-pr39-codex-1 | 사실 | 사실 | 판정 없음 | 사실 | 사실 |
| synctx-vux1-codex2-1 | 사실 | 사실 | 사실 | 판정 없음 | 판정 없음 |
| synctx-vux2-codex-1 | 사실 | 사실 | 사실 | 사실 | 판정 없음 |

굵은 글씨가 「아님」 오채택이다. 「판정 없음」은 사람 목록(설계 결정·판단 불가)이다. 나머지 24건은 네 구성이 같았다.

- rsbe-pr676-c0(잠금 파일 끝 개행)은 기준선을 포함해 세 구성이 「사실」로 채택했다. 기준선과 m-external은 작업자 하나가 「설계 결정」으로 보냈지만 Opus가 「사실」로 굳혔고, m-mix-sonnet-cursor는 두 작업자가 같은 패치로 합의했다. `m-mix-codex-haiku`만 Codex·Haiku 둘 다 「설계 결정」으로 보내 판가름 없이 사람에게 넘겼다.
- Cursor가 든 두 구성의 나머지 오채택(m-external pr723-w1·pr729-t3, m-mix-sonnet-cursor pr723-t6·pr729-t3)은 모두 Cursor 작업자(b)가 「사실·설계 결정 아님」으로 고치고, Opus 판가름이 「사실」로 굳힌 경우다(pr723-t6은 Sonnet도 「사실」). 지난 측정(2026-10-19)에서 본 외부 작업자의 약점(pr723-t5·t6·w1)이 같은 항목군에서 다시 나왔다.

## 비용

단가 기준: 2026-10-06 claude-api 스킬 자료(shared/prompt-caching.md, shared/model-migration.md) 기준, $/MTok. 캐시 읽기는 모델별 표기값(Haiku 5.5는 입력 단가의 0.1배), 캐시 쓰기는 5분 TTL 입력 단가의 1.25배·1시간 TTL 입력 단가의 2배. 구독 이용 한도가 이 추정 비용에 비례한다고 가정했다.

### 칸별 Claude 비용(끔)

| 칸 | m-claude-only | m-mix-codex-haiku | m-external | m-mix-sonnet-cursor |
| --- | ---: | ---: | ---: | ---: |
| code.a | Sonnet $6.38 | Codex | Codex | Sonnet $6.27 |
| code.b | Haiku $0.38 | Haiku $0.39 | Cursor | Cursor |
| code.adjudicator | Opus $6.90 (18회) | Opus $6.67 (17회) | Opus $10.93 (27회) | Opus $10.52 (26회) |
| **round1-core 합** | **$13.67** | **$7.06** | **$10.93** | **$16.79** |
| code.opinion(고르는 데 안 씀) | Fable $1.84 (2건) | Codex 1건(토큰 미기록) | Fable $2.18 (2건) | Fable $6.31 (6건) |

- Haiku 37건은 합쳐 $0.4다. Haiku를 Cursor로 바꿔도 Claude 비용은 줄지 않고, 오히려 Cursor 작업자와 Sonnet이 더 자주 갈려 판가름이 늘었다(18 → 26). `m-mix-sonnet-cursor`가 기준선보다 비싼 이유다.
- 의견·재검토는 계획서 2절대로 고르는 데 쓰지 않는다. 재검토 프롬프트는 m-claude-only 15건, m-mix-codex-haiku 15건, m-external 19건, m-mix-sonnet-cursor 15건이 생겼고 돌리지 않았다.

### 외부 CLI 사용량

| 구성 | Codex(입력 · 출력 · 캐시 읽기) | Cursor(입력 · 출력 · 캐시 읽기) |
| --- | --- | --- |
| m-mix-codex-haiku | 706만 · 7.7만 · 590만 (작업자 37건, 의견 1건 별도) | - |
| m-external | 692만 · 7.5만 · 580만 | 144만 · 24.5만 · 1,355만 |
| m-mix-sonnet-cursor | - | 145만 · 23.7만 · 1,348만 |

Cursor `composer-2.5`는 계획서 기준 호출당 약 $0.01이라 구성당 약 $0.4다.

### 추정과 실제

| 구성 | 추정(round1-core) | 실제 | 시간(확인 실행 포함) |
| --- | ---: | ---: | --- |
| m-claude-only | 약 $18 | $13.67 | 약 27분 |
| m-mix-codex-haiku | 약 $11~13 | $7.06 | 약 1시간 5분(의견 대기 제외) |
| m-external | 약 $13~14 | $10.93 | 약 1시간 50분 |
| m-mix-sonnet-cursor | 약 $18~20 | $16.79 | 약 1시간 50분 |

- 실제 Claude 합계(서브에이전트): round1-core $48.45 + 의견 $10.33 + 확인 실행 $0.42 = 약 $59.2. 실행 전 추정은 round1-core 약 $60~65와 의견 약 $15였다. 판가름 호출이 추정보다 적었다(18~27회).
- 이 메인 세션(Opus)이 진행을 맡은 비용은 따로 재지 않았다.
- 전체 경과 시간: 2026-10-22 21:43 ~ 2026-10-23 02:12(약 4.5시간). 다음 구성의 replay를 앞 구성의 Workflow와 겹쳐 돌렸다(사용자 승인). 한 건당 replay 시간은 Codex 약 1.4분, Codex+Cursor 약 2.5분, Cursor 약 2.3분이었다.
- 사용량 한도에는 걸리지 않았다.

## 측정 중 생긴 일

- **Workflow 묶음과 이름표.** 항목마다 Workflow를 띄우지 않고 구성의 작업을 한 Workflow로 묶었다. 표본 안에 같은 지적 id(`P1-1`, `P2-1`)가 두 항목씩 있어 이름표(`<지적 id> <역할>`)가 겹치므로, 겹치는 항목은 별도 Workflow로 돌렸다. `CF usage`는 항목마다 같은 Workflow 기록을 읽으므로, 다른 항목의 에이전트는 「일치하지 않음」으로 빠진다(경고만, 집계에 영향 없음).
- **인자 전달 사본.** 토큰을 줄이려고 `scripts/run-jobs.js`에서 인자 전달만 바꾼 사본(`eval/runs/2026-10-22-stage1/run-jobs-compact.js`, 프롬프트 경로를 공통 앞부분과 나머지로 나눠 받음)을 확인 실행 일부와 그 뒤 Workflow에 썼다. 에이전트에게 가는 프롬프트·이름표·모델·effort·스키마는 같다(에이전트 기록에서 프롬프트 문자열을 대조했다).
- **자동 모드 분류기가 커밋을 막음.** 이 세션이 자동 모드라 서브에이전트의 `git commit --no-verify`(작업자 규칙)가 막힌 경우가 있었다(m-claude-only pr676-c0 작업자 a·판가름, m-external pr676-c0 판가름). 패치는 커밋이 아니라 워크트리와 스냅샷의 차이(추적 안 된 파일 포함)에서 뽑으므로(`cascade/patches.py` `extract`) 판정·채택에는 영향이 없다.
- **판가름·의견이 네트워크·Docker를 씀.** rsbe-pr726-c0(rule_engine 설정 키 호환)에서 m-mix-codex-haiku 판가름이 `git ls-remote`·`gh pr view`로 사내 rule_engine 저장소를 조회하고 `docker run golang:1.26`(네트워크 없음)으로 구 엔진을 빌드해 재현했다. m-mix-sonnet-cursor 의견도 같은 저장소를 받아 확인했다. m-external pr720-c1 판가름은 로컬 mariadb 컨테이너를 띄워 IN 절 크기를 재현했다(끝난 뒤 정지·삭제했다고 보고). Claude 서브에이전트는 샌드박스 없이 세션 권한으로 돈다. dry-run에서도 이런 외부 접근이 생긴다는 점을 적어 둔다.
- **Cursor 작업자 패치에 임시 파일이 섞임.** m-external·m-mix-sonnet-cursor의 synctx P1-1·P2-1에서 Cursor 패치에 `.pytest-tmp/` 등 테스트 산출물 수천 줄이 들어 있었다. 판가름이 그 패치를 고르지 않아 결과에는 영향이 없다.
- **Codex 의견 순서.** m-mix-codex-haiku의 의견 1건(pr752-t3)은 Codex(`RR --opinion`)로 가므로, 같은 Codex 로그인을 쓰는 m-external replay(작업자 workers)와 겹치지 않게 그 replay가 끝난 뒤 돌렸다. 그동안 그 항목만 수집을 미뤘다.
- **브랜치 목록 경고.** m-claude-only judge 중 3건에서 「jobs 이후 사용자 저장소의 브랜치 목록이 바뀌었다」 경고가 나왔다. 같은 저장소에서 다음 구성의 replay가 워크트리를 만들던 때라 그 영향으로 보며, 작업 트리·HEAD·stash는 그대로라 계속했다.
- **채점표 경로 이름.** `score.py`의 채택 경로 「Codex 검토」는 의견이 붙은 항목을 세는데, 의견 runner가 Claude(Fable)여도 이 이름으로 센다(`eval/score.py:100`). 이번 표에서는 쓰지 않았다.

## 해석

- `m-mix-codex-haiku`는 기준선과 정확도가 같고(14/37) 「아님」 오채택은 더 적으며(0건), Claude 비용은 끔 52%, 켬 38% 수준이다. Sonnet 몫($6.4)이 빠지고 판가름 수는 거의 같다(18 → 17).
- `m-external`은 정확도가 가장 높지만(48.6%) 사람 목록이 적은 만큼 「아님」을 「사실」로 채택하는 일이 늘었다. 지난 측정과 같은 약점이다.
- 이번 정확도는 사람 목록을 「맞히지 못함」으로 세므로, 설계 결정 기준이 들어간 지금은 정답이 「아님」인 16건 대부분(12~16건)이 사람 목록으로 가 정확도가 낮게 나온다. 네 구성 모두 같은 조건이라 비교에는 문제가 없지만, 지난 측정(10-19)과는 기준선이 달라 직접 비교하지 않는다.
- 1회 측정이고 표본이 37건이라, 구성 사이 1~2건 차이는 흔들림 안에 있을 수 있다.

## 다음(2단계 후보 제안)

계획서 4절 2단계 규칙(거름을 넘은 후보 중 비용이 낮은 1~2개와 기준선을 두 번 더)에 따르면 후보는 `m-mix-codex-haiku` 하나다.

- 2단계 실행 대상: `m-claude-only` 2회 + `m-mix-codex-haiku` 2회(각 3회로 맞춤).
- 비용 추정(이번 실측 기준): Claude 약 $15.5 × 2 + 약 $7 × 2 = 약 $45(의견 포함), Codex 작업자 74건(입력 약 1,400만 토큰). 시간은 겹쳐 돌리면 약 2.5~3시간.
- 외부 전송: `m-mix-codex-haiku`의 code.a·code.opinion이 두 저장소 코드를 OpenAI로 보낸다. 실행 전에 다시 동의를 받는다.
- 정하지 않은 것(사용자 결정): 2단계로 갈지, `m-external`을 「아님」 오채택 대책 뒤 다시 잴지, 계획서 6절의 Claude 대안(Haiku+Haiku 등)을 넣을지.
