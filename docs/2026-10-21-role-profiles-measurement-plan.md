# 역할 프로필 측정 계획 (2026-10-21)

역할별 구성 프로필(`docs/superpowers/specs/2026-10-21-role-profiles-design.md`)의 후보를 같은 표본으로 재서 `local`과 `claude-only` 내장값을 정할 근거를 만든다. 초판 `docs/2026-10-18-workers-measurement-plan.md`를 잇는다. 지금까지의 측정은 모두 dry-run이고 한 번씩만 돌렸다.

- `docs/2026-10-20-workers-measurement-result.md`(claude/awesome-noether-5d49f1 브랜치)
- `docs/2026-10-20-doc-mode-workers-measurement-result.md`(claude/doc-mode-workers-measurement-16ba85 브랜치)
- `docs/2026-10-20-adopt-on-both-pass-measurement.md`

이 계획은 기본값을 바꾸지 않는다. 결과를 보고 내장값을 바꾸는 변경을 따로 연다.

## 1. 전제

- 프로필 구현(`--profile`, repos.toml `[profiles.*]`, `eval/replay.py --profile`)이 끝난 뒤에 돌린다. 섞는 조합(A=Codex·B=Haiku 등)은 프로필 없이는 만들 수 없다.
- 작업별 Claude 토큰 기록은 `CF usage`로 한다(사용자 결정). Workflow를 돌릴 때마다 `CF usage --run-dir <실행 폴더> --round N --workflow <Workflow 실행 id>`(첫 검토는 `--review-dir <review_dir>`, `init` 전에)를 돌리면 Workflow 기록 폴더(`~/.claude/projects/<프로젝트>/<세션>/subagents/workflows/wf_*/`)에서 작업별 토큰과 추정 비용을 읽어 `round-N/claude-usage.json`에 쓴다. 켬 값을 반사실로 다시 계산하려면 판가름이 빠진 항목의 Opus 토큰을 항목별로 빼야 하기 때문이다. 단계 합계로는 Sonnet·Haiku 몫도 나눌 수 없다.
- 다른 세션의 수정이 들어간 뒤에 돌린다. 수정 전 결과와는 기준선이 달라 직접 비교하지 않는다.
  - 빈 검증 통과(아무 테스트도 돌지 않은 검증을 통과로 보지 않음)
  - `eval/score.py` 통계(「둘 다 검증 통과」 사유 채택 집계)
  - 문서 판정자 설계 결정 기준(`prompts/worker-doc-judge.md`)
  - 코드 작업자 설계 결정 기준(`prompts/worker-code.md`, 커밋 `98794cc`). 2026-10-21 현재 `claude/worker-design-decision-criteria-4fa304`에만 있다. 측정 브랜치에서 `git merge-base --is-ancestor 98794cc HEAD`가 성공해야 한다
  - 위 수정마다 측정 브랜치에 들어간 커밋을 결과 문서에 적는다
- 측정 후보 구성은 repos.toml(측정용 사본, `CASCADE_REPOS_TOML`로 지정)의 `[profiles.*]`로 만든다. 측정마다 그 파일을 결과 폴더에 복사해 둔다.

## 2. 판정 기준(가성비)

사용자 결정: 정확도 하한을 넘은 구성 중 Claude 비용이 가장 적은 것을 고른다. 지난 기준(일치율 ≤5%p, Opus 호출 ≤ 기준, 대체 실행 ≤10%, 회귀 0)을 이것으로 바꾼다.

### 거르는 조건(모두 만족)

| 조건 | 정의 | 왜 |
| --- | --- | --- |
| 정확도 | 맞힌 수 / **표본 전체 수**가 기준선(`claude-only`)보다 5%p 넘게 낮지 않다. 오류·미완료·누락 항목은 「맞히지 못함」으로 센다 | 판정 없음(사람 목록)을 분모에서 빼던 일치율은 사람에게 많이 넘길수록 좋아 보였다. 지금 채점기는 오류·미완료·누락도 분모에서 빼서, 37건 중 36건이 오류여도 1건이 맞으면 100%가 된다. 그래서 분모를 표본 전체로 고정한다 |
| 완주 | 오류·미완료·누락 항목이 표본의 5% 이하(37건이면 1건 이하) | 실행이 많이 실패한 구성은 정확도·비용 모두 비교할 수 없다. 이 조건을 못 넘으면 「미완주」로 표시하고 거른다(내가 정함) |
| 「아님」 오채택 | 정답이 「아님」인데 「사실」로 채택한 수가 기준선보다 많지 않다 | 외부 구성의 약점(pr723-t5·t6·w1)이 바로 이것이다 |
| 대체 실행 | 외부 작업 중 대체 실행 비율 ≤ 10% | 지난 기준 그대로다 |
| 회귀 | 0건(4단계 전향 적용에서 본다) | 지난 기준 그대로다. 소급 재생은 dry-run이라 못 잰다 |

### 고르는 규칙

- 남은 구성 중 **Claude 비용(추정)**이 가장 적은 것을 고른다. Opus 호출 수 기준은 따로 두지 않는다. Claude 비용에 녹아 있기 때문이다.
- Claude 비용(추정) = Σ(모델별 Claude 토큰 × 그 모델 단가)다. 토큰은 `CF usage`가 쓴 작업별 사용량(1절)에서 읽는다. 단가는 `CF usage`가 기록할 때의 Anthropic 공개 가격(입력·출력·캐시 읽기·캐시 쓰기)이고, 기준 날짜와 출처가 `claude-usage.json`과 채점 결과의 「단가 기준:」 줄에 남는다. 구독 이용 한도가 이 값에 비례한다고 가정한다. 이 가정은 결과 문서에 적는다.
- 비용 범위는 1회차의 작업자·점검자·판가름이다(`score.py --cost-scope round1-core`). 의견·재검토는 판가름·채택 결과에 따라 생기므로 반사실 켬 값을 구할 수 없다(Codex 리뷰 지적 반영). 그래서 고르는 데 쓰지 않고, 끔 실행의 값만 따로 적는다.
- 판정은 `score.py`의 「선정 조건」 줄(설계 8절)로 쌍마다 낸다. 여러 후보·3회 평균을 모아 고르는 것은 결과 문서의 표에서 아래 규칙대로 한다.
- 비용 차이가 10% 이내인 구성끼리는 사람 목록으로 간 수가 적은 쪽을 고른다(내가 정함).
- 반복한 구성(4절 2단계)은 3회 평균으로 거르고 고른다. 최소·최대도 함께 적는다. 3회 중 한 번이라도 정확도 조건을 못 넘으면 결과 문서에 「불안정」으로 표시한다(내가 정함).
- 오류로 끝난 항목은 채점 전에 원인을 보고 한 번만 다시 돌린다(지난 측정의 사용자 저장소 변경 검사 오탐처럼 측정 환경 탓인 경우). 다시 돌려도 오류면 그대로 「맞히지 못함」으로 센다. 다시 돌린 항목과 이유는 결과 문서에 적는다.
- 기준을 낮춰 통과시키지 않는다. 남는 구성이 없으면 `local`은 잠정값 그대로 두고, 원인을 보고한 뒤 사용자가 정한다.

## 3. 매 실행 전에 받을 것과 기록할 것

실행할 때마다(단계마다, 반복마다) 사용자에게 받는다.

- [ ] 그 실행의 승인
- [ ] 외부 전송 동의. 저장소별로 받는다. 이번 실행에서 어느 칸이 어디로 보내는지(OpenAI·Cursor 팀 프라이버시 모드)를 적어서 묻는다. 보내면 안 되는 저장소는 표본에서 뺀다
- [ ] 비용 추정. 아래 표의 값을 그 실행에 맞춰 다시 계산해 보인다. Claude 서브에이전트 토큰, Codex·Cursor 토큰, 걸리는 시간이다. Cursor `composer-2.5`는 호출당 약 $0.01이고, Auto 등 다른 Cursor 모델은 쓰지 않는다
- [ ] `codex`·`cursor-agent` 로그인 상태

실행마다 결과 폴더(`eval/runs/<날짜>-<단계>/<구성>/`, git 제외)에 남긴다.

- `profiles.toml`(그 실행의 repos.toml 사본)
- 걸린 시간(단계별 시작·끝 시각)

## 4. 단계

비용을 보고 단계를 나눈다. 단계마다 결과를 보고하고, 다음 단계는 사용자가 정한다.

### 1단계: 코드 모드 소급 재생 1회

- 표본: 지난 측정과 같은 코드 모드 37건(사실 20 · 아님 16 · 재현 불가 1). 지난 결과 폴더는 `~/01.workspace/backup/cascade-fix-eval-runs/2026-10-19/`로 옮겨져 있다(`sample.jsonl`, `repos.toml`, `claude/`·`external/` 결과). 표본이 가리키는 저장소 sample-be·synctx는 `~/01.workspace/10.repository/`에 있다. 측정할 때는 이 폴더를 새 결과 폴더로 복사해 쓰고, 백업은 고치지 않는다
- 구성(사용자 결정)

| 이름 | code.a | code.b | 판가름 |
| --- | --- | --- | --- |
| `claude-only`(기준선) | Sonnet low | Haiku low | Opus high |
| `mix-codex-haiku`(= `local` 잠정값) | Codex low | Haiku low | Opus high |
| `external` | Codex low | Cursor `composer-2.5` | Opus high |
| `mix-sonnet-cursor` | Sonnet low | Cursor `composer-2.5` | Opus high |

- 채택 규칙은 **끄고** 돌린다. 켠 값은 같은 실행에서 반사실로 다시 계산한다(사용자 결정). 지난 `adopt-on-both-pass` 측정과 같은 방법이다. 규칙을 켜고 돌리면 규칙으로 채택한 항목에 Opus가 돌지 않아 끈 값을 구할 수 없다. 반대 방향은 된다.
- **채택 규칙을 끄는 방법**: 내장 `claude-only`·`local`은 `adopt_on_both_pass = true`이고, 명령줄 `--adopt-on-both-pass`는 켜기만 한다. 그래서 측정용 repos.toml 사본에 끈 측정 프로필을 따로 만든다. replay에는 `--adopt-on-both-pass`·`--workers`를 주지 않는다(주면 프로필 칸을 덮는다).

```toml
# <결과>/profiles.toml — 원래 repos.toml의 [repos.*]를 복사하고 아래를 더한다
[profiles.m-claude-only]
base = "claude-only"
adopt_on_both_pass = false

[profiles.m-mix-codex-haiku]
base = "local"
adopt_on_both_pass = false

[profiles.m-external]
base = "claude-only"
adopt_on_both_pass = false
"code.a" = { runner = "codex" }
"code.b" = { runner = "cursor", model = "composer-2.5" }

[profiles.m-mix-sonnet-cursor]
base = "claude-only"
adopt_on_both_pass = false
"code.b" = { runner = "cursor", model = "composer-2.5" }
```

  - 구성마다 두 번에 나눠 돌린다(Codex 리뷰 지적 반영). replay는 표본 전체를 돌고, 외부 작업도 그 자리에서 실행한다(`eval/replay.py:149-152, 231-240`). 그래서 확인을 먼저 끝내야 한다.

```
# 1) 확인 실행: 첫 항목 하나만, 따로 둔 폴더에
CASCADE_REPOS_TOML=<결과>/profiles.toml python3 eval/replay.py --sample <결과>/sample.jsonl --out <결과>/check/m-claude-only --profile m-claude-only --limit 1
# 2) 확인이 끝나면 본 실행: 표본 전체
CASCADE_REPOS_TOML=<결과>/profiles.toml python3 eval/replay.py --sample <결과>/sample.jsonl --out <결과>/m-claude-only --profile m-claude-only
```

  - 두 명령은 `jobs`(외부 작업은 `workers`)까지 하고 멈춘다(`eval/replay.py:153-161`, `meta.json`에 `pending_run_dir`). 확인 실행과 본 실행 모두 `eval/sample-format.md` 「돌리는 순서」대로 끝까지 잇는다(Codex 리뷰 지적 반영).
    1. 항목마다 남은 Claude 작업(`stage1-args.json`, 있으면 `stage1-fallback-args.json`)을 스킬의 Workflow로 돌려 결과를 저장하고, 끝날 때마다 `CF usage --run-dir <pending_run_dir> --round 1 --workflow <Workflow 실행 id>`를 돌린다(이후 판가름·의견 Workflow도 같다).
    2. 각 명령의 「다음:」 출력을 따라 `judge` → (판가름이 있으면 Workflow로 `adj.json`) `adjudicate` → (판단 불가가 있으면) 의견 → `apply` → `report`로 잇는다. 의견은 건너뛰지 않는다(Codex 리뷰 3회차 지적 반영). 판가름 뒤 의견 단계가 있기 때문이다(`cascade/cli_steps.py:743`). 끔 실행의 의견 비용도 이 단계에서 기록한다(2절).
       - 재검토는 돌리지 않는다. 비교는 1회차만 보기 때문이다(내가 정함). `apply`가 재검토 프롬프트(`round-1/recheck-prompt.md`)를 만들었으면 그 사실만 결과 문서에 센다.
    3. `python3 eval/replay.py --sample … --out <같은 폴더> --collect`로 모은다. `--collect`가 원장과 `run/`을 항목 폴더로 복사하고 워크트리를 지운다.
    4. `--collect` **전에** 항목마다 완료를 확인한다. `--collect`는 마지막 회차의 `apply.json`만 보고 워크트리를 지우기 때문이다(`eval/replay.py:195`). 그 뒤에는 남은 단계를 이어 갈 수 없다. 완료 조건은 셋이다.
       - `round-1/apply.json`과 보고서(`report.md`)가 있다.
       - `CF status --run-dir <pending_run_dir>`의 「다음:」에 남은 의견 단계가 없다.
       - 재검토 말고는 남은 단계가 없다.
    5. 수집 뒤 모든 항목 폴더에 `ledger.json`이 생겼는지 본다. 채점(`score.py --round 1`)은 그 뒤에 한다.
  - 확인 실행에서 볼 것. 위 순서를 `--collect`까지 끝낸 뒤, replay가 옮겨 찍은 init·jobs 출력(설계 5절)과 원장으로 확인한다.
    - `프로필: m-claude-only (--profile)`이고 `덮음:` 줄이 없다.
    - 원장의 `adopt_on_both_pass`가 거짓이다.
    - **jobs 뒤 확정 배정**(`profile.roles`의 `code.a`·`code.b`·`code.adjudicator`)이 위 후보 표와 같고 `profile.substituted`가 비어 있다. 다르면 로그인·샌드박스 점검 같은 원인을 고친 뒤 확인 실행부터 다시 한다(내가 정함). init·jobs에서 대체된 채로 돌리면 이름만 같고 다른 구성을 재게 된다.
  - 본 실행은 첫 항목도 다시 돈다. 1건(1/37)이 중복되는 비용을 받아들인다(내가 정함). 확인 실행 결과는 채점에 넣지 않는다.
  - 본 실행 뒤에도 항목마다 확정 배정을 후보 표와 대조한다. init·jobs 단계에서 대체된 항목이 있으면 그 항목을 그 구성으로 다시 돌린다. 작업 실행 중 대체(한도·시간 초과 등)는 구성의 실제 동작이므로 그대로 두고, 「대체 실행」 조건으로 센다.
  - `m-mix-codex-haiku`는 `local`을 시작점으로 삼으므로, 측정 전에 `local` 내장값이 이 계획의 잠정값(코드 A=Codex low, B=Haiku low)과 같은지 본다. 사용자 repos.toml이 `local`을 덮고 있으면 그 값도 함께 따라오므로, 측정용 사본에는 원래 파일의 `[profiles.*]`를 복사하지 않는다.
- 결과 표에는 구성마다 끔·켬 두 값을 나란히 둔다. 2절 규칙은 켬 값에 적용한다(프로필 기본값이 켬이므로). 끔 값은 규칙 효과를 보는 데 쓴다.
- 켬 값의 비용은 1회차 작업자·점검자·판가름 범위(`--cost-scope round1-core`)에서, 규칙으로 판가름이 빠진 항목의 Opus 작업별 토큰을 빼서 구한다. 의견·재검토는 켬 값에 넣지 않는다(2절).
- **반사실 재계산의 검증**(Codex 리뷰 3회차 Suggestion 반영). 방법은 지난 측정(`docs/2026-10-20-adopt-on-both-pass-measurement.md` 「잰 방법」)과 같다. 항목마다 작업자 a·b 결과와 검증 결과(기준선 실패 명령 제외)로 `decide()`를 끄고 한 번, 켜고 한 번 돌린다. 보안 경로와 빈 패치 처리(`without_empty_patch`)는 `judge`와 같게 거친다. 다음 셋을 모두 맞춰야 켬 값을 쓴다.
  - 끈 재계산이 저장된 원장 판정의 분기와 모든 항목에서 같다. 판가름이면 `adj-args.json`이 있고, 아니면 없다.
  - 켠 결과가 끈 결과와 다른 항목 수가, 켬에서 빠진 판가름 호출 수와 같다.
  - 뺀 Opus 토큰이 그 항목들의 작업별 판가름 토큰 합과 같다.
  - `python3 eval/adopt_recalc.py --runs <결과>/<구성> --out <결과>/<구성>/adopt-recalc`로 돌린다. `--out`에 항목별 끔·켬 결정(`decisions.jsonl`), 위 세 검증의 대조 결과(`check.json`, 하나라도 어긋나면 종료 코드 1), 켬 값으로 바꾼 채점용 폴더, `counterfactual.json`이 들어간다. 스크립트는 `cascade`의 `decide()`를 그대로 불러 쓴다. 켠 폴더는 `score.py --cost-scope round1-core`로만 비용을 낸다.
- 구성별 채점과 고르기. 구성마다 `python3 eval/score.py --runs <결과>/<구성> --baseline-runs <결과>/m-claude-only --sample <결과>/sample.jsonl --round 1 --cost-scope round1-core --json <결과>/<구성>/score.json`을 낸다(켬 값은 `--runs`에 그 구성의 `adopt-recalc` 폴더를 주고 기준선도 같은 쪽을 줘서 끔은 끔끼리, 켬은 켬끼리 짝을 맞춘다. 그래서 기준선 `m-claude-only`에도 `adopt_recalc.py`를 돌려 둔다). 2단계의 반복까지 모이면 `python3 eval/select_profiles.py --candidate <구성>=<점수 JSON>[,…] … --baseline m-claude-only`가 2절 규칙대로 평균·최소·최대와 거름 사유, 선정을 낸다. `--baseline`은 `--candidate`로 준 이름 중 하나여야 하고, 끔 값과 켬 값은 따로 한 번씩 부른다. 사용법은 `eval/sample-format.md`에 있다.
- 빠진 것: Claude 대안(Haiku+Haiku, effort 조정)은 이번 단계에서 뺐다(사용자 결정). 그래서 이 계획으로는 `claude-only`를 채택 규칙 효과 말고는 바꿀 근거가 생기지 않는다. 넣을지는 2단계 뒤에 다시 정한다.

| 구성 | Claude 토큰(추정) | 외부 | 시간(추정) |
| --- | ---: | --- | --- |
| `claude-only` | 약 740만 | - | 약 30분 |
| `mix-codex-haiku` | 약 450만~520만 | Codex 37작업(입력 약 700만) | 약 1.5~3시간 |
| `external` | 약 260만 | Codex 37 · Cursor 37작업 | 약 3시간 |
| `mix-sonnet-cursor` | 약 450만~520만 | Cursor 37작업(약 $0.4) | 약 1~2시간 |
| **합계** | **약 1,900만~2,000만** | | |

- 추정 근거: 지난 실측. 기준선은 stage1 547만 + 판가름 191만이다. 섞는 조합은 Sonnet·Haiku 몫을 반씩 나누고 판가름을 기준선 수준으로 잡았다. 실제 값은 판가름 횟수에 따라 크게 달라진다.

### 2단계: 반복(흔들림)

- 1단계에서 2절 거르는 조건을 넘은 후보 중 비용이 낮은 1~2개와 기준선을 두 번 더 돌려 3회로 맞춘다(사용자 결정: 단계적).
- 흔들림 지표
  - 항목별 판정이 실행 사이에 바뀐 비율(같은 구성 3회 안에서)
  - 정확도·「아님」 오채택·Opus 호출·Claude 비용의 최소~최대
- 비용: 기준선 2회 약 1,500만에, 후보 1~2개 × 2회(약 500만~1,000만 × 개수)를 더한다. 합계 약 2,000만~3,500만 토큰이다.

### 3단계: 문서 모드와 첫 검토(전향)

사용자 결정: 정답은 실제 행동으로 사후에 정한다. 문서 모드 작업자는 `claude-only`와 섞는 조합을 나란히 돌린다.

- **표본 모으기**
  - 대상: 초안 단계의 설계·계획 문서(「설계 합의 완료」 전). 지난 문서 측정은 합의가 끝난 문서라 7건이 모두 「사실」 쪽이었다.
  - 문서를 실제로 검토할 때마다 첫 검토를 셋 다 돌린다: Claude(Fable 5.1 high, `CF review --profile claude-only --root <저장소> --kind doc --doc <문서>`), Codex(`RR --repo <저장소> --doc <문서>`), Cursor(`CR --repo <저장소> --doc <문서> --model composer-2.5`). 세 명령 모두 같은 저장소 루트를 명시한다(Codex 리뷰 5회차 지적 반영). `--repo`가 없으면 `RR`·`CR`은 문서가 있는 폴더의 저장소를 기준으로 삼고(`docs/codex-review-changes/run-review.sh:56`), `CF review`는 현재 디렉터리의 저장소를 본다. 문서가 저장소 밖에 있으면 셋이 서로 다른 코드를 보게 된다. Claude 첫 검토는 프로필을 명시해 Fable로 고정한다(Codex 리뷰 지적 반영). 사용자 기본 프로필이 `local`이면 `CF review`가 Codex 안내로 바뀌기 때문이다(설계 5절).
  - 세 결과의 지적을 합쳐 중복을 지운다(같은 위치·같은 문제면 하나로 친다). 누가 찾았는지는 지적마다 남긴다. 중복 판정은 메인 세션이 하고, 애매한 것은 사용자에게 보인다. `python3 eval/first_review.py merge --claude … --codex … --cursor … --doc <문서> --out <폴더>`가 묶음 후보를 만들고, 사람이 `merged.json`을 확정한 뒤 `merge --confirm`이 cascade-fix 입력(`findings-input.json`)과 누가 찾았는지(`found-by.json`)를 쓴다. 검토자마다 결과를 자기 폴더(각자의 `review_dir`)에 둔다. 결과 옆의 `review-meta.json`이 그 검토자의 이력으로 붙기 때문이다.
  - 합친 목록으로 cascade-fix를 실제로 한 번 돌린다(`init --findings-json`). 이 실행의 결과와 사용자의 반영·철회·선택이 정답이다. 정답을 정하는 방식은 코드 표본과 같다(`eval/sample-format.md`). 합친 입력의 실제 실행 보고서에는 첫 검토 이력이 없고 첫 검토 비용은 `first_review.py score`로 본다.
  - 실제 실행의 구성은 문서마다 `claude-only`와 `doc-mix`를 번갈아 쓴다(사용자 결정). 실제 실행의 결과를 보고 사용자가 고르므로 정답이 그 구성 쪽으로 기울 수 있다. 번갈아 쓰면 그 기울어짐이 두 구성에 고르게 나뉜다. 결과 문서에는 항목마다 어느 구성이 실제 실행이었는지 적고, 구성별로 나눈 표도 함께 보인다.
  - 다른 구성은 같은 입력으로 `--dry-run` 돌려 비교한다.
  - **기준 시점을 하나로 고정한다**(리뷰 지적 반영). 실제 실행이 문서를 고친 뒤 다른 구성을 돌리면 서로 다른 문서를 비교하게 된다. 사본을 따로 만들면 git 밖 문서의 경로(`외부/<파일명>`, `cascade/cli.py:155-170`)와 함께 보이는 파일이 실제 실행과 달라진다(Codex 리뷰 지적 반영). 그래서 두 구성을 모두 **원래 저장소에서 같은 상태로** 돌린다(내가 정함).
    1. 첫 검토 셋을 돌리기 전에 HEAD, `git status --porcelain` 결과, 대상 문서 내용 해시를 결과 문서에 적는다. 이것이 기준 상태 S다.
    2. 첫 검토 셋, dry-run 구성, 실제 실행을 이 순서로 S에서 돌린다. dry-run은 작업 트리를 건드리지 않으므로(`init --dry-run`) 실제 실행보다 먼저 돌리면 두 구성이 같은 S를 본다. 그사이 사용자가 저장소를 바꾸지 않도록 알린다.
    3. 두 구성의 init은 저장소 전체를 스냅샷 커밋으로 만든다(`cascade/snapshot.py`). 두 실행의 스냅샷 트리 해시(`git rev-parse <스냅샷 커밋>^{tree}`)가 같아야 비교에 넣는다. 다르면(그사이 무엇이 바뀌었으면) 그 문서는 S를 새로 잡고 다시 돌리거나 표본에서 뺀다. 이 해시가 같으면 작업자 워크트리에 들어간 문서 경로와 참조 파일도 같다.
    4. 비교와 채점은 1회차 판정만 본다. `score.py --round 1 --pre-final`(설계 8절)로 채점한다. `--pre-final`은 실제 실행만 거치는 전체 검사가 판정을 바꾼 몫을 빼고 비교하기 위한 것이다. 실제 실행의 전체 검사 실패는 4단계와 같은 방식으로 회귀로 따로 적는다(Codex 리뷰 5회차 지적 반영). 실제 실행에서만 생기는 재검토 회차와 그 지적은 비교에 넣지 않고, 의견·재검토 기록(아래)으로만 쓴다.

| 이름 | 판정자 a · 작성자 | 판정자 b · 점검자 |
| --- | --- | --- |
| `claude-only` | Sonnet low | Haiku low |
| `doc-mix` | Codex low | Haiku low |

  - `doc-mix`는 내장 프로필이 아니다. 측정용 repos.toml에 만든다. `doc.judge_a`와 `doc.writer`는 다른 칸이라 둘 다 바꾼다(Codex 리뷰 지적 반영). 채택 규칙은 문서 모드에서 쓰지 않으므로 그대로 둔다.

```toml
[profiles.doc-mix]
base = "claude-only"
"doc.judge_a" = { runner = "codex" }
"doc.writer" = { runner = "codex" }
```

  - 확인: 실행마다 init의 `역할:` 줄과 jobs 뒤 원장의 `profile.roles`를 위 표의 **그 구성 줄**과 대조한다(Codex 리뷰 5회차 지적 반영). `claude-only`는 `doc.judge_a`·`doc.writer`가 Sonnet low, `doc-mix`는 codex(low)다. 두 구성 모두 `doc.judge_b`·`doc.checker`는 Haiku low이고 `profile.substituted`는 비어 있어야 한다. 다르면 1단계와 같이 원인을 고치고 다시 돌린다.
  - **채점 입력**: 문서 하나를 한 번 실행하면 지적 여러 개가 한 원장에 들어간다. `score.py`는 지적마다 폴더를 읽고 그 폴더의 실행 전체를 세므로, 같은 실행을 지적별 폴더에 그대로 연결하면 호출 수·사용량이 지적 수만큼 겹쳐 잡힌다(Codex 리뷰 지적 반영). 그래서 설계 8절의 「실행 단위 표본」으로 채점한다. 표본 줄마다 `run`(실행 폴더)과 지적 id를 적으면, 판정은 지적마다 보고 호출 수·사용량은 실행마다 한 번만 센다.
  - **채점용 폴더로 옮기기**(Codex 리뷰 3회차 지적 반영). 일반 CF 실행은 `.cascade-fix/<실행 id>/`에 남고, `cleanup` 뒤에는 워크트리가 지워진다. 그래서 두 구성 모두 `report`까지 끝낸 뒤 `cleanup` 전에 실행 폴더를 `eval/runs/<날짜>-doc/<구성>/<run>/`로 통째로 복사한다(`cp -R`). `<run>`은 실행 id가 아니라 문서마다 정한 이름(`<문서 이름>-<날짜>`)이다. 두 구성의 실행을 같은 `<run>` 이름 아래 둔다(Codex 리뷰 4회차 지적 반영). 표본 줄의 `run`은 이 상대 이름이다. `score.py --runs eval/runs/<날짜>-doc/doc-mix --baseline-runs eval/runs/<날짜>-doc/claude-only`가 구성마다 다른 폴더에서 찾는다. `score.py`는 이 폴더의 `ledger.json`과 `round-*`를 직접 읽는다(설계 8절). 표본 파일은 `eval/runs/<날짜>-doc/sample.jsonl`에 지적마다 한 줄씩 `{"id", "run", "finding": {"id"}, "truth"}`를 적는다.
  - 30건 이상, 「아님」이 섞일 때까지 모은다. 문서 수와 기간은 정하지 않는다. 지적 수가 쌓이면 중간 보고를 한다.
- **첫 검토 지표**(검토자마다)
  - 정밀도 = 그 검토자 지적 중 정답 「사실」 / 그 검토자 지적 수
  - 재현율 = 그 검토자가 찾은 「사실」 / 합친 목록의 「사실」 전체
  - 등급별(Critical·Warning·Suggestion)로도 나눈다. 비용(Fable 토큰, Codex·Cursor 토큰)도 함께 적는다. 확정한 묶음과 정답 파일로 `python3 eval/first_review.py score --merged <확정한 merged.json> --truth <정답.jsonl>`이 검토자별 정밀도·재현율·비용을 낸다(Claude 첫 검토는 `CF usage --review-dir`로 만든 `claude-usage.json`, Codex는 토큰 기록 없음).
- **코드 모드 첫 검토**도 같은 방식으로 실제 코드 검토(`--base`·`--uncommitted`) 때 세 검토자를 돌려 모은다. 코드 작업자 비교는 1·2단계가 맡으므로 여기서는 첫 검토 지표만 본다(내가 정함).
  - 명령: Claude(`CF review --profile claude-only --root <저장소> --kind code --base <브랜치>`), Codex(`cd <저장소> && RR --base <브랜치>`), Cursor(`CR --repo <저장소> --base <브랜치> --model composer-2.5`). `--uncommitted`도 같은 꼴이다.
  - Codex만 `cd`로 들어간다. codex-review의 코드 모드는 `--repo`를 무시하고 현재 디렉터리의 저장소를 검토하기 때문이다(문서 모드와 cursor-review는 `--repo`를 따른다). `CF review`가 외부 첫 검토를 안내할 때도 같은 접두를 붙인다.
- **의견·재검토**는 이 단계의 실제 실행에서 생기는 대로 기록만 한다. 기록할 것은 의견이 판가름과 다른 비율과 재검토가 새 지적을 낸 비율이다. 판정 기준에는 넣지 않는다(내가 정함. 표본이 판단 불가 항목에서만 생겨 적다).
- 비용(추정): 지적 1건에 `claude-only` 약 28만, `doc-mix` 약 12만, Fable 첫 검토가 문서당 약 20만이다. 30건과 문서 5개 정도면 Claude 약 1,300만이다. 이 중 실제 검토 한 번에 원래 드는 몫을 빼면 추가분은 약 절반이다.

### 4단계: 회귀(전향 적용)

- 1~3단계로 고른 `local` 후보를 실제 작업(dry-run 아님)에 쓴다. 다음 실제 실행 10회 또는 채택 패치 50건 중 먼저 닿는 쪽까지다(내가 정함).
- 회귀는 둘 중 하나다.
  - apply 뒤 전체 테스트가 간섭 의심으로 실패
  - 채택 패치가 버그 때문에 같은 PR 안에서 사람에게 되돌려지거나 다시 고쳐짐
- 회귀가 하나라도 있으면 원인을 보고하고, 그 후보를 내장값으로 올리지 않는다.
- `python3 eval/regress.py --runs <실행 폴더>… [--reverts <되돌림.jsonl>]`가 프로필별로 센다. 실행 폴더는 시간 순서로 주고, 되돌려진 패치는 줄마다 `{"run", "finding", "reason"}`으로 적는다. 종료 조건(실행 10회·채택 패치 50건)에 먼저 닿은 쪽이 출력에 나온다.
- 추가 비용이 없다(실제 작업 비용). 다만 외부 전송 동의는 실행마다 받는다.

## 5. 산출물

- 단계마다 결과 문서 `docs/<날짜>-role-profiles-<단계>-result.md`. 지난 결과 문서 형식을 따른다. 표본, 구성, 결과 표(끔·켬), 정답별 표, 비용, 측정 중 생긴 일, 다음을 담는다.
- 결과 폴더(`eval/runs/…`)는 저장소 코드와 지적 원문이 들어 있어 커밋하지 않는다. 공유 전에 확인하고, 필요 없으면 지운다.
- 4단계까지 끝나면 `local`·`claude-only` 내장값을 바꾸는 변경을 따로 연다.

## 6. 정하지 않은 것

- Claude 대안(Haiku+Haiku, 판가름 Opus medium 등)을 측정에 넣을지. 2단계 뒤 사용자가 정한다.
- 문서 모드 `external`(Codex+Cursor)을 다시 잴지. 문서 판정자 기준 수정 뒤 Cursor 판정자의 설계 결정 표시가 줄었는지는 이 계획으로는 확인하지 않는다.
