# (a-full) 설계: Claude 작업을 `claude -p` 러너로 (2026-10-28, 초안)

`docs/2026-10-28-orchestration-cost-design.md`의 (a-full) 후보를 구체화한 설계 초안이다. 이 문서의 「구현됨」은 (a-lite)와 (a-full) 1차(11절)다. 11절에 적은 범위 밖의 (a-full)은 설계 단계이고 유료 측정은 하지 않았다. (a-lite)는 PR #31로 main에 병합됐다(병합 커밋 c918ff4, 이 브랜치의 구현 커밋 20688b9. 병합 사실은 세션 메시지 기준이고 이 작업 트리의 로컬 `main`에는 아직 없다). 읽은 것은 `cascade/runners.py`·`cascade/workers.py`·`cascade/jobs.py`·`cascade/cli.py`·`cascade/cli_steps.py`·`cascade/report.py`·`scripts/run-jobs.js`·`eval/solo.py`와 `claude --help`(2.1.311, 모델 호출 없음)다.

## 0. 구현 상태

| 구분 | 내용 | 상태 |
| --- | --- | --- |
| (a-lite) | `CF start`(init→jobs까지)·`CF go`(Workflow 결과 저장·`usage`·`judge`~`report`를 멈출 곳까지)·`next_actions`(다음 단계를 구조화해 `next_steps`가 문자열로 그림)·`workers.lock`(실행 폴더당 `workers` 하나)·모델 ID 경고 멈춤·끝나지 않은 단계의 Workflow 결과 교체·`eval/orchestration_cost.py`의 `start`/`go` 인식·SKILL.md 「기본 경로」·`tests/test_cli_go.py`·`go.lock`·검토 스크립트 실패 처리(로그 없이 종료 코드 77이면 로그인 안내로 멈춤, 그 밖의 비정상 종료는 stderr와 함께 오류로 멈춤)·Claude 검토자 Workflow 결과의 모델 ID 사전 검사 | 구현됨, main에 병합(PR #31). 내용은 전체 테스트 1,597개 통과 시점의 작업 트리 기준이다 |
| (a-full) 1차 | `ClaudeRunner`, `--claude-via cli`(1단계 작업자·점검자), CLI 사용량 기록, 예상 Claude 작업 수집 | 구현됨(11절). 유료 측정 전이라 기본값은 `workflow` |
| (a-full) 2차 | 판가름(`workers --stage adj`)·Claude 의견(`workers --stage opinion`)의 CLI 경로 | 구현됨(11.2.1). 재검토는 격리 경로 검증 전까지 Workflow 유지 |
| (a-full) 나머지 | 재검토·첫 검토의 CLI 경로(읽기 전용 전용 경로), 재개·`--retry-failed`·중단 경계, 격리 점검 | 설계 단계 |

(a-lite)에서 이 설계가 이어 쓰는 것: `go`의 단계 이음 루프, `Action` 구조, `workers.lock`, 모델 ID 경고 멈춤(출력 줄 검사 방식), Workflow 결과 검증(작업 id 일치).

## 1. 한 줄 요약

지금 Claude 작업(작업자 B, 판가름, 의견·재검토·첫 검토)은 메인 세션이 Workflow 도구로 띄운다. 이를 Codex·Cursor처럼 `CF workers`가 `claude -p`로 띄우는 `ClaudeRunner`로 바꾸면, 이미 있는 `go`가 Workflow 없이 `report`까지 이을 수 있어 메인 세션 요청이 약 5건으로 준다(지난 문서 추정). 단 이것은 검토 칸(의견·재검토)이 Codex·Cursor인 프로필(`local`·`local-sonnet-b`)에서 작업 역할만 옮겨도 성립하는 값이고, 검토 칸이 Claude인 프로필(`claude-only` 등)은 검토 역할까지 옮겨야 성립한다(3.2). 위험은 작업 격리·사용량 기록·모델 ID 검증·재개 경계·사용자 설정 오염·Workflow 동의 문구에 있다. Workflow 경로는 지우지 않고 남긴다.

## 2. 지난 설계안과 달라진 점

- (a-full)이 있으면 메인 세션 요청이 5건으로 줄어 메인 세션의 맥락(약 345k)이 그대로여도 지적당 약 $0.13이다(지난 문서 추정, 미측정). 그러면 서브에이전트(d)는 더해도 $0.13 → $0.09 정도라 **(d)를 하지 않아도 목표 $0.34를 넘는다**. 목표 조건은 「(a-full) 뒤 현재 세션 맥락에서 지적당 $0.34 아래」로 정했고 (d)는 하지 않는다.
- 진행 명령은 새 `CF run`을 만들지 않고 이미 구현한 `go`를 확장한다(4절).

## 3. 현재 구조와 바꿀 곳

| 구분 | 지금 | 바꾼 뒤(설계) |
| --- | --- | --- |
| Claude 작업 실행 | 메인 세션이 `scripts/run-jobs.js`를 Workflow로 실행, 결과는 `go --workflow-output`이 저장 | `CF workers`가 `ClaudeRunner`(새)로 실행, 결과를 `*-cli.json`과 같은 모양으로 저장 |
| 출력 스키마 | Workflow `agent(..., {schema})` | `claude -p --json-schema`(`eval/solo.py`가 이미 이 방식, `structured_output` 우선·없으면 JSON 블록 추출) |
| 사용량 기록 | `CF usage`가 Workflow 기록 폴더를 읽는다 | 호출마다 `claude -p`의 JSON 봉투(`total_cost_usd`, `session_id`)와 세션 기록으로 `claude-usage.json`을 바로 쓴다(5.2) |
| 실패·대체 | Workflow가 `null`을 돌려주면 메인이 처리 | 기존 `Outcome`·`_Exhaustion` 체인을 쓰되 Claude 대체 실행·재개 규칙을 새로 정한다(5.5) |
| 동시 실행 | Workflow 내부 | `run_all`의 CLI별 최대 4개 풀(Claude도 한 칸) |

이미 있어서 이어 쓰는 것: `CliJob`·`Outcome`·`run_all`·`classify_failure`·`jobs.py`의 `_job`(`runner`·`role`·`worktree`·`writable`을 `workers`가 있을 때 싣는다)·`split_job_args`.

**기존 코드가 그대로는 안 맞는 곳**(코드를 열어 확인한 것):

1. 판가름은 `workers` 단계가 아니다. `workers --stage`는 `stage1`·`checker`뿐이고(`cascade/cli.py` 파서), 판가름 작업(`build_adjudicator_jobs`)은 `_job(f.id, "adjudicator", prompt)`로만 만들어져 `role`·`runner`·`worktree`·`writable`을 싣지 않는다. `workers`는 입력 뒤 `judge`가 끝났으면 거절하는데(`judged_after_workers_input`), 판가름은 바로 그 `judge` 뒤에 돈다. 판가름 전용 단계(`workers --stage adj`)에는 입력·완료 조건·작업 메타데이터(판가름은 패치를 쓰므로 `writable=true`)·결과 병합과 재실행 규칙이 필요하다.
2. 외부 CLI 실패 뒤 Claude 대체는 Workflow를 요구한다. `_rerun_with_fallbacks`는 fallback 대상이 Claude면 실행하지 않고(`runner != RUNNER_CLAUDE`만 다시 돈다), 남은 작업은 `fallback_job`이 `*-fallback-args.json`(Workflow용, 워크트리·쓰기 여부 없음)으로 쓴다. `--claude-via cli`가 최초 작업만 바꾸면 이 경로는 여전히 Workflow가 필요하다.
3. 모델 ID 검증: `_check_output`은 결과의 `model_id`를 `"<runner>:<요청 모델>"`로 덮어쓴다. Claude 작업은 `claude:claude-haiku-5-5`가 되는데 `expected_model_id`의 Claude 기대값은 접두사 없는 `claude-haiku-5-5`다(메모리 안 호출로 확인). 그대로 연결하면 정상 결과도 경고가 나거나 실제 모델 불일치를 숨긴다(5.6).
4. 사용량 누락 계산: 보고서는 `CLAUDE_JOB_FILES`(Workflow 입력 파일 목록)에서 예상 Claude 작업을 찾아 기록 누락을 센다. 이 목록에 `stage1-cli-args.json`·`checker-cli-args.json`이 없어, CLI 경로에서 Workflow 입력 파일을 만들지 않으면 사용량 기록이 실패해도 「미측정」으로 세지 못하고 「Claude 작업 없음」으로 보인다(5.2).

### 3.1 실행 경로를 정하는 시점과 작업 파일 변환

- `start`는 `init` 뒤 곧바로 `jobs`를 돌려 작업 파일을 만든다. 그래서 `--claude-via`는 `start`·`init` 인자로 받아 원장에 기록하고 `jobs`가 읽는다. `jobs` 뒤에 바꾸려면 `jobs --force`(판정·적용 전)로 작업 파일을 다시 만든다(5.8).
- 작업 파일: `_job`은 `workers`가 있으면 `runner`·`role`·`worktree`·`writable`을 싣지만, `split_job_args`가 Claude 작업에서 `runner`·`role`·`worktree`·`writable`(`_CLAUDE_JOB_HIDDEN`)을 지워 Workflow용 `stage1-args.json`을 만든다. CLI 실행에는 이 메타데이터가 필요하다. 규칙: `cli`일 때는 Claude 작업도 CLI 작업 파일(`stage1-cli-args.json`)로 보내 메타데이터를 보존하고, `stage1-args.json`은 `jobs: []`로 둔다(`has_jobs`가 거짓이라 Workflow 단계가 생기지 않는다). `_CLAUDE_JOB_HIDDEN`은 Workflow용 파일에만 적용한다.
- 판가름은 이 메타데이터가 아예 없다(3절 1번). `cli`일 때 판가름 입력 파일(예: `adj-cli-args.json`)에 `role=adjudicator`·`writable=true`·`worktree`(판가름 워크트리)·`kind`를 싣는 규칙을 새로 정한다.
- 대체 실행 작업(`fallback_job`)도 같은 규칙으로 워크트리·쓰기 여부를 보존한다(3절 2번).

### 3.2 모드·역할별 이전 범위와 남는 Workflow 단계

| 모드 | 역할 | 대상 | 쓰기 | 이전 |
| --- | --- | --- | --- | --- |
| 코드 | `code.a`·`code.b`(작업자 A·B) | 작업 워크트리 | 둘 다 쓴다 | 1차 |
| 코드·문서 | 판가름(`adjudicator`) | 판가름 워크트리(패치를 커밋) | 쓴다 | 1차 → **11.2로 대체됨**(1차 구현에서 제외, Workflow 유지) |
| 문서 | `judge_a`·`judge_b` | 작업 워크트리 | 읽기 | 1차 |
| 문서 | `writer` | 작업 워크트리 | 쓴다 | 1차 |
| 문서 | `checker` | 작업 워크트리 | 읽기 | 1차 |
| 공통 | 첫 검토(`review`) | 사용자 저장소 | 읽기 | 2차 |
| 공통 | 의견(`opinion`) | base 워크트리(없으면 저장소 루트) | 읽기 | 2차 |
| 공통 | 재검토(`recheck`) | 저장소 루트(dry-run은 `result-rN` 워크트리) | 읽기 | 2차 |

- 코드 모드 A는 프로필에 따라 Claude(`claude-only`·기본)이거나 Codex(`local`)다. 그 칸이 Claude일 때만 이 러너를 쓴다. 문서 모드에서 B(`judge_b`)는 읽기 역할이고 쓰기 역할은 `writer`다. 지난 초안의 「쓰기 역할(B·판가름)만 먼저」는 이 표와 맞지 않아 1차 범위를 「작업 워크트리에서 도는 모든 Claude 칸」으로 정정한다. 읽기 역할도 작업 워크트리 안이라 `_run_isolated`의 읽기 역할 규칙(변경을 발견하면 결과 무효)을 그대로 쓸 수 있다.
- 2차(검토 역할)는 사용자 저장소를 읽으므로 5.1의 전용 경로가 정해진 뒤에 옮긴다.
- **1차만 옮긴 뒤 남는 Workflow 단계**: 검토 칸이 Claude이면 의견·재검토(와 첫 검토, 대체 실행)가 Workflow로 남는다. `claude-only`는 의견·재검토가 Claude(Fable)라 `report`까지 Workflow 없이 가지 못한다. 검토 칸이 Codex·Cursor인 `local`·`local-sonnet-b`는 `report`까지 Workflow가 없다(Codex 의견·재검토는 `go`가 스크립트로 돌린다). 즉 「Workflow 없이 `report`까지」·5건·$0.13은 후자 프로필에서 1차만으로 성립하는 값이다.

## 4. 진행 명령: `go` 확장 (별도 `run` 명령은 만들지 않는다)

지난 초안은 `CF run`을 새로 만들자고 했으나 `go`가 이미 단계 이음·멈춤·Workflow 결과 저장을 한다. 두 대안을 비교했다.

| 대안 | 장점 | 단점 |
| --- | --- | --- |
| `go`에 `--claude-via cli`를 더한다(권장) | 이음 루프·멈춤 규칙·테스트를 재사용, 진행 로직이 한 곳 | `next_actions`가 Claude 작업을 Workflow 대신 `workers` 단계로 내도록 바뀐다 |
| 별도 `run` 명령 | 기존 `go`를 건드리지 않는다 | 이음 루프·멈춤 규칙을 복제해 두 벌을 유지 |

설계: `--claude-via cli`(원장에 기록)일 때 `next_actions`가 Claude 작업 단계를 `Action(kind="workflow")` 대신 `workers` 단계(1차 범위: `stage1`·`checker`·`adj`. **`adj`는 11.2로 대체됨** — 구현된 1차는 `stage1`·`checker`만이다)의 `cmd` 행동으로 낸다. 의견·재검토·첫 검토는 2차 범위라 검토 칸이 Claude이면 Workflow 행동이 그대로 남는다. 1차 범위만 Claude인 프로필에서는 `go`의 기존 「workers와 Workflow를 동시에 띄운다」 멈춤이 사라지고, `go`가 `workers`를 직접 돌려(`workers.lock` 아래) `report`까지 한 번에 이어 간다. 멈추는 곳은 (a-lite)와 같다(종료 코드 4·5·6, PR 재현 명령 승인, 모델 ID 경고, 로그인 만료, 보류 후보 선택, `report` 뒤).

## 5. 설계 쟁점과 제안

### 5.1 작업 격리 (가장 큰 위험)

- 지금 Workflow 서브에이전트는 메인 세션의 권한 모드로 돈다. `claude -p`는 헤드리스라 도구 권한을 플래그로 줘야 한다. `eval/solo.py`는 `--permission-mode acceptEdits --allowedTools Bash --setting-sources project --strict-mcp-config`를 쓴다. 이 조합은 Bash가 홈·다른 저장소·네트워크에 닿을 수 있어 Codex 샌드박스(`workspace-write`)나 Cursor `-p --sandbox enabled`보다 약하다. 지금도 사후 검사는 있다(`judge`·`adjudicate`의 사용자 저장소 상태 검사, 종료 코드 6).
- **`_run_isolated`는 폐기 가능한 작업 워크트리에서만 쓴다.** 이 함수가 쓰는 `tree_of`는 대상 폴더의 실제 인덱스에 `git add -A`를 실행하고, `restore_tree`는 `git add -A`·`read-tree -u --reset`·`git clean -f -f -d`로 작업 내용을 되돌린다. 작업자·판가름의 `r1-xx-a` 같은 워크트리는 버려도 되므로 괜찮다. 그러나 **사용자 저장소를 읽는 역할(첫 검토, 일반 실행의 재검토·의견)에 그대로 쓰면** 모델이 아무것도 쓰지 않아도 사용자의 스테이징 상태가 바뀌고, 실행 중 생긴 사용자 변경을 복구 대상으로 취급할 수 있다. 첫 검토 대상은 사용자 저장소이고(`cmd_review`), 재검토 대상도 보통 저장소 루트다(`recheck-repo.txt`).
- 제안: 사용자 저장소를 읽는 역할은 `_run_isolated`를 쓰지 않는 전용 경로로 둔다. 읽기 전용 도구만 허용하고(`--tools`·`--disallowedTools`), 실행 전후 비교는 인덱스를 쓰지 않는 방식(`git status --porcelain` 출력과 `git diff` 해시 비교 등)으로 하며 **복구는 하지 않고 변경을 발견하면 결과를 무효로 하고 사용자에게 알리기만 한다**. 구체 방식은 1단계에서 확인한다(미확인). 이 역할의 CLI 경로는 구현 순서에서 뒤로 미룬다(7절).
- 작업 워크트리에서 도는 역할(코드 a·b, 문서 writer·judge·checker, 판가름. 3.2): 도구를 줄이고(WebFetch·MCP 제외) 쓰기 범위를 워크트리로 묶는 설정을 `--settings` JSON으로 줄 수 있는지 확인해야 한다. `claude --help`에 `--settings`·`--tools`·`--disallowedTools`·`--add-dir`이 있는 것까지만 확인했고, 쓰기 범위 설정 키(샌드박스 등)가 있는지는 확인하지 못했다. 판가름은 로컬 컨테이너·다른 저장소를 쓴 선례(10-26 측정)가 있어 도구를 더 열어야 한다.
- 확인 방법: Cursor ACP 격리 점검(`cascade/cursor_acp_check.py`)처럼 카나리아 파일(워크트리 밖 읽기·쓰기, 홈 민감 경로, 네트워크)을 두고 모델이 누출하는지 본다. 모델 호출이라 소액 유료다.
- 판단: 격리가 Workflow 서브에이전트와 같은 수준임을 보이지 못하면 `--claude-via cli`를 기본으로 하지 않는다.

### 5.2 사용량 기록

- `claude -p --output-format json`은 `total_cost_usd`·`session_id`·`num_turns`를 준다. 지난 측정에서 이 값은 세션 기록을 `cascade.usage` 단가로 센 값과 같았다(111세션, 차이 0). `ClaudeRunner`는 `Outcome.usage`에 토큰(세션 기록의 `message.usage`, 메시지 id마다 마지막 값)을 담고 `claude-usage.json`의 작업별 행으로 쓴다. `--no-session-persistence`는 주지 않는다(세션 기록이 비용의 근거다).
- **하위 에이전트**: `read_session(path, subagents=False)`는 하위 에이전트 기록을 세지 않는다(`eval/solo.py`는 `subagents=True`로 센다). 새 러너가 Agent 도구를 허용하면 비용이 빠진다. 제안: 작업자·판가름·검토 역할에는 Agent 도구를 주지 않는다(`--disallowedTools`). 허용하기로 하면 `subagents=True`로 세고 JSON 봉투 비용과 대조한다. 허용 여부는 결정 항목이다.
- **미측정 계산**: 보고서의 예상 Claude 작업 목록(`CLAUDE_JOB_FILES`)이 Workflow 입력 파일만 본다. CLI 경로의 Claude 작업도 찾도록 `stage1-cli-args.json`·`checker-cli-args.json`에서 `runner == claude`인 항목을 수집하게 `report.py`를 바꾸거나, `ClaudeRunner`가 실행 전에 예상 작업 목록을 따로 기록해야 한다. 기록 파일 누락·손상·시간 초과에서도 비용이 0이나 「작업 없음」이 아니라 「미측정」으로 나오는지 테스트한다.
- **병렬 쓰기**: 지금 `claude-usage.json`은 읽고 합쳐 다시 쓰는 방식이다(`_usage_record`가 읽은 뒤 `write_json`이 파일을 통째로 쓰고, `write_json`은 원자적이지 않다). Workflow 경로는 `CF usage`가 한 번에 한 프로세스로 돌지만, 병렬로 도는 `ClaudeRunner` 작업이 같은 파일을 읽고 쓰면 서로 덮어쓴다. 제안: 작업마다 별도 파일(`round-N/usage/<작업 id>.json`)에 임시 파일 뒤 이름 바꾸기로 원자 저장하고, `workers`가 끝난 뒤 단일 작성자(`workers` 종료 단계 또는 `go`)가 합쳐 `claude-usage.json`을 쓴다. 합치기는 작업 id로 중복을 걸러 다시 해도 두 번 더해지지 않게 한다.
- 보고서 「역할별 사용량」·`CF usage`: 새 경로에서는 `CF usage`가 필요 없고(러너가 직접 기록), `--claude-via workflow`일 때만 쓴다.

### 5.3 사용자 설정 오염

- 작업자 프롬프트에 사용자 `~/.claude/CLAUDE.md`(Serena·agentmemory·한국어 지침 등)가 실려 비용과 행동이 달라질 수 있다. solo.py는 `--setting-sources project`를 줬지만 사용자 `CLAUDE.md`는 그래도 실렸다(지난 측정 기록). `--bare`(훅·자동 메모리·`CLAUDE.md` 자동 탐색 생략)는 후보로 평가한다. 도움말에 OAuth·키체인 인증을 건너뛴다고 적혀 있어 이 기기의 로그인 방식에서 쓸 수 있는지 확인이 필요하다(미확인).
- 환경 변수: 참고 구현 `eval/solo.py`는 자식 `claude -p`의 환경에서 `CLAUDECODE`를 제거한다(`child_env`). `go`는 Claude Code의 Bash 도구에서 돌아 `CLAUDECODE`를 물려받는다. 같은 처리를 하는 것이 안전하지만 제거하지 않으면 실제로 문제가 되는지는 확인하지 못했다(미확인, 6절 1단계에서 확인).
- 정한 것: 사용자 `CLAUDE.md`가 작업자에 실리는 것은 받아들이고 `--bare`는 평가한다. 동등성 측정(6절)에서 비용·판정 차이를 본다.

### 5.4 Workflow 동의 문구

- SKILL.md는 「계획 확인(또는 /cascade-fix 호출)이 곧 Workflow 도구를 쓰라는 사용자의 요청」이라고 한다. `claude -p`는 Claude 호출을 사용자 기기에서 별도 프로세스로 띄우는 것이라 같은 말로 덮이지 않는다. 계획 확인에 「Claude 작업을 CLI(`claude -p`)로 돌린다」를 한 줄 더하고, 그 줄이 있을 때만 `--claude-via cli`를 쓴다.
- 프라이버시: Claude는 이미 Workflow로 같은 코드를 보고 있어 새 수신처가 아니다(Codex·Cursor와 달리 외부 전송 동의 대상이 늘지 않는다).

### 5.5 병렬·시간·한도·재개·중단

- 병렬·시간: `run_all`의 CLI별 동시 4개, 작업당 900초를 그대로 쓴다. 첫 검토·의견은 Opus·Fable급이라 길어 역할별로 시간 제한을 따로 둔다.
- **한도·로그인 뒤 재개는 지금 규칙과 맞지 않는다.** `_Exhaustion`은 한도·인증 실패를 runner 단위로 기억해 이후 작업을 건너뛰고, 그 상태는 마지막 `jobs` 이후의 `workers-*` 이벤트에서 다시 읽는다(`exhausted_runners`). `_workers`는 실패 작업이 있어도 단계 완료를 기록한다(`s.mark(rnd, step, …)`를 무조건 부른다). 그래서 로그인을 고친 뒤 같은 명령을 다시 부르면 「이미 했다」로 거절되고, `--force`로 다시 해도 소진 상태가 남아 그 runner를 건너뛴다(SKILL.md: `jobs --force`로 회차를 다시 열어야 다시 부른다). 설계가 필요한 것: ① 복구 가능한 중단 상태와 단계 완료의 구분 ② 인증 복구·한도 해제 뒤 소진 상태를 푸는 방법 ③ 실패한 작업만 다시 돌리는 방법(성공 결과와 이전 비용 보존) — 예: `workers --retry-failed`가 해당 runner의 소진 기록을 지우고 `stage1-cli.json`의 비성공 작업만 다시 돌린다. 이는 새 명령 계약이므로 결정 항목이다.
- Claude 한도·인증: `claude -p`의 사용 한도·인증 실패 문구가 봉투(`is_error`·`result`)나 stderr에 어떻게 나오는지 확인하지 못했다(미확인, 「아직 실측하지 않은 것」과 같은 항목). 모르면 오류로 분류해 fallback(`claude-only` 값, 곧 같은 Claude)으로 가는데, Claude 한도에 걸렸으면 대체도 못 하므로 멈추도록 별도 처리가 필요하다.
- **중단 경계(미확인)**: `_execute`는 자식을 `start_new_session=True`로 띄우고 시간 초과일 때만 프로세스 그룹을 끝낸다. 부모(`CF workers`/`go`)가 강제 종료되면 자식 `claude -p`가 남을 수 있고, `workers`는 전체 실행이 끝난 뒤에야 결과 파일과 완료 이벤트를 쓰므로 중간 결과가 사라진다. (a-lite)가 더한 `workers.lock`은 `flock`이라 프로세스가 죽으면 풀려 중복 실행은 막지만 자식 정리와 부분 결과 보존은 하지 않는다. 설계가 필요한 것: 중단 신호 처리와 자식 그룹 정리, 작업 하나가 끝날 때마다 결과를 저장해 재개 때 완료 작업을 보존하는 기준. 검증은 모델 호출 없이 가짜 CLI를 실행 도중 종료하는 테스트로 한다(6절 0단계).

### 5.6 모델·모델 ID

- 요청 모델·실제 모델·표시용 ID를 분리한다. `claude -p`가 모델 별칭을 어떻게 풀어 실제 모델 ID를 보고하는지(봉투·세션 기록 어디에 나오는지)는 확인이 필요하다(미확인). 실제 모델을 기대값(`expected_model_id`의 접두사 없는 Claude 값)과 비교하고, 불일치하면 결과 채택 전에 막는다.
- **경고 뒤 멈춤은 종료 코드로는 안 된다.** `cmd_judge`·`cmd_adjudicate`는 모델 ID 경고를 출력한 뒤 판정과 원장 기록을 진행하고 0을 반환한다. (a-lite)의 `go`는 단계 출력의 「경고: … 모델 ID」 줄을 검사해 멈추는 방식으로 이를 처리한다(출력 문자열 의존이고, 멈출 때는 이미 그 단계 기록이 끝났다). (a-full)에서는 `ClaudeRunner`가 불일치 결과를 채택하지 않도록 실행기 안에서 막는 것이 더 안전하고, `judge`·`adjudicate`에 전용 종료 코드 또는 `--strict-model`을 두는 안이 있다. 결정 항목이다.

### 5.7 판정 품질

- 프롬프트·스키마·모델·effort가 같고 호출 방식만 다르다. 이론상 같지만 도구 목록·시스템 프롬프트·설정 소스가 달라 행동이 바뀔 수 있다(예: 사용자 `CLAUDE.md` 유무). 동등성 측정(6절)으로 본다.

### 5.8 되돌림 범위

「Workflow 경로를 남긴다」가 보장하는 것과 보장하지 않는 것을 나눈다.

- **새 실행의 기본값 복귀**: `--claude-via workflow`로 새 실행을 시작하는 것은 보장한다(경로가 코드에 남아 있다).
- **진행 중 실행의 전환**: 경로는 `jobs` 때 정해지고(3.1), 성공한 CLI 결과는 Claude 결과보다 뒤에 합쳐져 같은 작업 id를 덮는다(`load_stage_results`: 같은 id는 뒤가 앞을 덮는다). 그래서 CLI 결과가 남은 채 Workflow로 바꾸면 옛 CLI 결과가 우선한다. 전환은 `jobs --force`로만 하며, 이 명령은 그 회차의 CLI·대체 실행 결과와 이전 판정·의견·판가름 상태를 지운다. **이미 `apply`한 회차는 `jobs`로 다시 열 수 없다**(`_reopen_round`). 따라서 `apply` 뒤에는 경로를 바꿀 수 없고, 그 실행은 시작한 경로로 끝낸다.
- 위의 보장은 실행 단위다. 롤백 보장 범위(새 실행 기본값만 / 판정 전 진행 중 전환까지)는 결정 항목이다.

## 6. 측정 계획(유료, 승인 필요)

| 단계 | 내용 | 비용 추정 | 통과 기준 |
| --- | --- | ---: | --- |
| 0. 무료 | 가짜 `claude` 실행 파일로 `ClaudeRunner`와 `go --claude-via cli`를 단위·e2e 테스트(`tests/fake_cli.py` 방식 재사용). 격리·사용량 기록(누락·손상·시간 초과가 「미측정」으로 나오는지)·한도 분류·모델 ID 불일치 차단·실행 중 강제 종료 뒤 재개를 확인 | 0 | 기존 테스트 + 새 테스트 통과 |
| 1. 소액 | 격리 점검(카나리아 읽기·쓰기·네트워크, 판가름 도구 접근), `claude -p` 봉투 모양·한도 문구·모델 ID 보고 방식 확인 | 수 달러 이하 | 누출 0건, 봉투 값이 세션 기록 합과 같음 |
| 2a. 예비(12건) | 표본 12건을 CLI 경로로 1회. 동작·비용이 크게 어긋나지 않는지 본다. **동등성 증명이 아니다** | CLI 1회 약 $4~6(37건 회차 비용의 약 1/3 환산, 추정) | 오류·누출 없음, 작업 비용이 기존의 ±20% 안 |
| 2b. 동등성(37건) | 같은 표본 37건을 CLI 경로로 3회(기존 Workflow 경로 3회와 같은 횟수) | CLI 3회 약 $33~54(회당 켬 $11~12·끔 $16~18) | 아래 기준 |
| 3. 진행 비용 | 실제 사용 5회 이상에서 `eval/orchestration_cost.py`로 측정(`start`/`go` 인식은 (a-lite)에서 처리). 단 `find_runs`는 `report`가 없는 실행을 버리므로 완료·중단·재시도를 함께 기록하도록 고친다(아래) | 사용 자체 | 성공한 실행 기준 지적당 진행 비용 $0.34 아래, 전체 시도 비용도 함께 보고 |

- **비교군 재사용**: 기존 `local-sonnet-b` 3회(2026-10-26, Workflow 경로, 같은 표본 37건)를 비교군으로 쓰면 새 비용은 CLI 경로분만이다. 다만 모델 서버 쪽 변화는 통제하지 못한다(`docs/2026-10-26-cost-effectiveness.md` 한계). 비교군을 새로 돌려 같은 조건으로 맞추면 두 경로 합계가 2b 기준 약 $66~108이고(Workflow 3회 + CLI 3회), 1회씩만 돌리면 약 $22~36(37건)·약 $8~12(12건)이다. 어느 쪽으로 할지가 결정 항목이다. 회차별 비용 값은 위 문서의 `local-sonnet-b` 표(끔 $16.34~18.46, 켬 $11.03~12.10)에서 가져왔고 새 경로의 작업 비용이 같다는 가정이며, 이 가정을 검증하는 것이 이 단계다.
- **동등성 통계량**: 기존 「흔들림」(`local-sonnet-b` 8.1%·3건, `local` 5.4%)은 같은 경로를 3회 돌렸을 때 판정이 한 번이라도 바뀐 항목의 비율이다. 경로별 1회씩의 불일치율은 다른 통계량이라 8.1%와 바로 비교할 수 없고, 12건 중 1건만 달라도 8.3%, 0건이어도 「8.1% 이하」임을 입증하지 못한다. 제안: 비교 단위는 「37건 항목별 3회 다수결 판정」이고, 지표는 ① CLI 3회 사이에 판정이 바뀐 항목 비율(기존 8.1%와 같은 정의) ② 두 경로의 다수결 판정이 다른 항목 수다. 허용 차이(예: ①이 기존 값 이하 + 1~2건, ②가 3건 이하 — 숫자는 사람이 정한다)는 측정 전에 고정한다.
- **정확도·오채택·완주는 별도 조건**: 판정 일치·흔들림만 보면 CLI 경로가 「아님」을 사실로 더 많이 채택하거나 더 자주 끝내지 못해도 통과할 수 있다. 기존 선택 기준(`eval/score.py`의 `selection()`)을 CLI 경로 각 회차에 그대로 적용해 조건마다 따로 통과해야 한다: 정확도(기준선에서 정해진 %p 이내), 「아님」 오채택(`false_fact` ≤ 기준선), 완주(오류·미완료·누락 비율 상한), 대체 실행 비율 상한(회귀는 재지 않았으면 제외). 기준선은 기존 Workflow 3회(또는 새 비교군)이고, 오채택은 건수라 3회 합산 기준과 허용 숫자는 사람이 정한다.
- **진행 비용 측정의 집계 범위**: `find_runs`는 `report`가 없는 실행(중단·실패·재시작 전 실행)을 버려 그 실행에 쓴 진행 비용이 빠진다. 3단계에서는 완료·중단·재시도를 모두 기록하고 「성공한 실행의 지적당 비용」과 「전체 시도 비용」을 같이 보고한다(재시작은 처음 시작 시각부터 이어 세는 지금 규칙을 유지).
- 작업 비용 비교는 같은 표본의 회당 평균으로 한다. 12건 예비 시험은 2b를 대신하지 않는다.

## 7. 구현 순서(승인 뒤)

(a-lite)는 구현됨. 이어서:

1. 계약 정리(코드 변경 최소): 실행 경로 확정과 작업 파일 변환(3.1), 판가름 `workers --stage adj` 계약(3절 1번), Claude 대체 실행 경로(3절 2번), 모델 ID 검증(5.6), `report.py`의 예상 Claude 작업 수집과 사용량 작업별 원자 저장·합치기(5.2). 테스트 먼저.
2. `ClaudeRunner`(`cascade/runners.py`)와 단위 테스트, 격리 점검·소액 확인(6절 0~1단계). 격리가 약하면 여기서 멈추고 결정으로 돌아온다. **1차: 작업 워크트리에서 도는 Claude 칸(코드 a·b, 문서 judge_a·judge_b·writer·checker, 판가름)만 옮긴다(3.2).**
3. `--claude-via cli` 옵션과 `go` 확장(4절). 재개·중단 규칙(5.5).
4. SKILL.md 절차에 CLI 경로를 더하고 Workflow 경로는 남긴다(되돌림 범위는 5.8).
5. 동등성·진행 비용 측정(6절 2~3단계), 통과하면 기본값을 `cli`로 바꾸고 추천 표를 갱신(별도 결정).
6. (2차) 검토 역할(첫 검토·의견·재검토)의 CLI 경로: 5.1의 전용 경로가 정해진 뒤. 이것이 끝나야 검토 칸이 Claude인 프로필도 Workflow 없이 `report`까지 간다(3.2).

## 8. 위험 요약

| 위험 | 영향 | 줄이는 법 |
| --- | --- | --- |
| 격리가 Workflow보다 약함 | 작업자가 워크트리 밖·네트워크 접근 | 도구 제한·설정, 카나리아 점검, 통과 못 하면 기본값 전환 안 함 |
| 읽기 역할에 `_run_isolated` 사용 | 사용자 인덱스·작업 내용 훼손 | 작업 워크트리에서 도는 역할(3.2의 1차)에만 적용, 사용자 저장소를 읽는 역할은 전용 경로·2차 |
| 모델 ID 검증 불일치 | 정상 결과에 경고 또는 실제 불일치를 숨김 | 요청·실제·표시 ID 분리, 실행기에서 채택 전 차단 |
| 사용자 설정(`CLAUDE.md`·훅·MCP)이 작업자에 실림 | 비용·행동 변화 | `--setting-sources`·`--bare` 평가, 동등성 측정 |
| 판정이 바뀜 | 정확도·오채택 | 동등성 측정(같은 정의의 흔들림 비교) |
| 한도·로그인 실패 뒤 재개 불가 | 같은 명령으로 이어지지 않음 | 복구 가능 중단 상태 구분, 실패 작업만 재시도(5.5) |
| 사용량 기록 실패가 「작업 없음」으로 보임 | 비용 과소 표시 | 예상 작업 목록에 CLI 입력 포함, 누락 테스트(5.2) |
| 부모 강제 종료 | 자식 잔존, 부분 결과 소실 | 신호 처리·자식 정리·작업별 결과 저장, 가짜 CLI 종료 테스트 |
| 작업 비용이 Workflow보다 큼 | 이득 상쇄 | 예비·동등성 측정에서 작업당 비용 비교 |
| 병렬 작업이 공용 `claude-usage.json`을 읽고 덮어씀 | 비용 누락·덮어쓰기 | 작업별 파일 원자 저장, 단일 작성자 합치기(5.2) |
| 경로 확정 시점이 `jobs` 뒤 | 작업 파일에 메타데이터 없음 | `start`·`init`에서 정해 원장에 기록, `jobs`가 변환(3.1) |
| 동등성 기준이 오채택·미완료 증가를 허용 | 품질 저하를 통과시킴 | `selection()` 조건을 각각 적용(6절) |
| 진행 비용 측정이 중단 실행을 버림 | 비용 과소 평가 | 완료·중단·재시도 모두 기록, 성공당·전체 시도 비용 보고 |

## 9. 사람이 정할 결정

정한 것(2026-10-28):

1. 목표 조건은 「(a-full) 뒤 현재 세션 맥락에서 지적당 $0.34 아래」이고 서브에이전트(d)는 하지 않는다.
2. 격리 기준은 Workflow 서브에이전트와 같은 수준이다. 못 맞추면 (a-full)을 접고 (a-lite)만 남긴다.
3. `--claude-via cli`를 도입하되 기본은 `workflow`다. 측정 통과 뒤에 기본을 바꾸고 Workflow 경로는 남긴다.
4. 계획 확인에 「Claude 작업을 CLI로 돌린다」 문구를 더한다(5.4).
5. 사용자 `CLAUDE.md`가 작업자에 실리는 것은 받아들이되 `--bare`는 평가한다(5.3).

남은 것:

6. 진행 명령: `go` 확장(권장) 대 별도 `run`(4절).
7. 사용자 저장소를 읽는 역할의 CLI 경로를 (a-full) 범위에 넣을지, 넣는다면 전용 경로 방식(5.1).
8. 새 러너의 Agent 도구 허용 여부와 비용 집계 범위(5.2).
9. 한도·로그인 뒤 재개 계약: `workers --retry-failed`(또는 같은 효과의 방식)를 새로 둘지(5.5).
10. 모델 ID 불일치 처리: 실행기 안 차단 + 전용 종료 코드·`--strict-model` 여부(5.6).
11. 동등성 측정 설계: 비교군을 기존 3회로 재사용할지 새로 돌릴지, 허용 차이 숫자, 예비 12건을 둘지(6절).
12. 6절 1~2단계 유료 측정 예산 승인(위 표 추정 기준).
13. 실행 경로를 정하는 시점과 작업 파일 규칙: `start`·`init` 인자로 받아 원장에 기록하고 `jobs`가 변환한다(3.1).
14. 1차 이전 범위: 작업 워크트리에서 도는 모든 Claude 칸을 한 번에 할지, 코드 모드 먼저(3.2). 2차(검토 역할)까지 끝나야 `claude-only` 같은 프로필이 Workflow 없이 가므로, 목표 프로필(`local`·`local-sonnet-b`)을 1차로 충분하다고 볼지.
15. 사용량 기록 방식: 작업별 파일 + 단일 작성자 합치기(권장) 또는 잠금(5.2).
16. 롤백 보장 범위: 새 실행 기본값만 / 판정 전 진행 중 전환까지(5.8).
17. 동등성 조건: 판정 일치·흔들림 + 정확도·오채택·완주·대체 실행 조건을 각각 둘지, 허용 숫자(6절). 진행 비용 측정에서 중단·재시도를 함께 보고하는 집계 방식.

## 10. 한계

- 미확인: `claude -p`의 쓰기 범위 제한 설정, 한도·인증 실패 문구, `--bare`와 이 기기 로그인, 모델 ID 보고 방식, 부모 강제 종료 때 자식 `claude -p` 동작, 사용자 저장소를 읽는 역할의 인덱스를 쓰지 않는 비교 방식.
- `go`의 5요청·진행 비용 $0.13은 지난 문서의 가정이다. 곁가지(보류 후보, 재검토, 다음 회차)가 있으면 더 든다.
- 새 경로의 작업 비용이 Workflow와 같다는 것은 가정이다. 둘 다 맥락이 빈 새 에이전트라 비슷할 것으로 보지만 시스템 프롬프트·도구 정의 크기가 달라 측정 전에는 모른다.
- 이 문서는 (a-lite) 구현 이후의 코드를 기준으로 하지만, (a-full) 관련 코드 판단은 읽기만 한 것이고 실행으로 재현한 것은 모델 ID 접두사 불일치(3절 3번) 하나다.

## 검토에서 남은 지적 (Codex 3회차, 2026-10-28)

이 설계는 초안이다. Codex 교차 리뷰를 세 회차 돌려 지적을 모두 확인해 반영했지만 회차마다 새 빈틈이 나와 0건에 수렴하지 않았다. 마지막 회차의 지적을 아래에 그대로 남긴다. 이 문서와 `docs/2026-10-28-orchestration-cost-design.md`를 함께 본 결과다. (a-full)을 구현하기 전에 이 목록을 점검한다.

Critical

없음.

Warning

1. 작업 ID 기준 사용량 중복 제거가 재시도 비용 보존 요구와 충돌한다.
   위치: docs/2026-10-28-claude-runner-design.md:93
   근거: 사용량 파일을 `<작업 id>.json`으로 저장하고 작업 ID로 중복 제거하지만, 110행은 실패 작업 재시도 시 이전 비용 보존을 요구한다. `cascade/cli.py:948`도 최초 실행과 대체 실행에 같은 작업 ID를 사용한다. 제안대로라면 서로 다른 유료 호출이 하나로 합쳐지거나 덮어써진다.
   제안: 작업 ID와 호출 시도 ID를 분리한다. 시도별 비용은 모두 합산하고, 동일 시도 기록을 재수집할 때만 중복 제거한다. 실패→대체→재시도 비용 보존을 테스트한다.

2. 진행 중 경로 전환에 실행 중인 작업을 멈추거나 배제하는 조건이 없다.
   위치: docs/2026-10-28-claude-runner-design.md:128
   근거: 문서는 `jobs --force`로 전환하도록 한다. 그러나 `cascade/cli.py:783`의 `cmd_jobs`는 `workers.lock`을 잡지 않고, 803행에서 작업 워크트리를 초기 상태로 복구한다. 실행 중인 `_workers`는 967행에서 원장을 다시 읽고 완료 이벤트를 추가한다. 따라서 전환과 기존 작업의 결과 저장이 충돌할 경로가 있다. 실제 동시 실행 재현은 미확인이다.
   제안: 경로 전환을 `go`·`workers`와 직렬화하고, 기존 자식 프로세스 종료를 확인한 뒤 초기화한다. 입력 세대 ID를 결과에 넣어 전환 전 결과를 거부하는 테스트도 추가한다.

3. 사용량 누락 보완 범위에서 실제 측정에 사용하는 평가기가 빠져 있다.
   위치: docs/2026-10-28-claude-runner-design.md:92
   근거: 제안은 `report.py` 수정 또는 별도 예상 작업 목록 기록이다. 하지만 `eval/score.py:321`의 `claude_jobs()`와 361행의 `collect_usage()`도 Workflow 입력 목록으로 누락을 검사한다. 메모리 내 모의 입력으로 Claude CLI 작업만 있고 사용량이 없는 경우를 확인했으며, 누락 표지와 누락 칸이 모두 비었다. 보고서만 고치면 평가기의 비용 누락 검사는 그대로 남는다. 새 `adj-cli-args.json`도 수집 대상에 명시되어 있지 않다.
   제안: 보고서와 평가기가 공유하는 예상 작업 수집 계약을 정의하고, 판가름·대체 실행까지 포함한다. 사용량이 전부 빠진 CLI 실행을 평가기가 반드시 미측정으로 처리하도록 검증한다.

4. 기존 대체 실행 비율을 그대로 적용하면 실행 경로에 따라 통과 기준이 달라진다.
   위치: docs/2026-10-28-claude-runner-design.md:143
   근거: `eval/score.py:160`은 `workers-*` 이벤트의 작업 수를 합산하고, 435행은 이를 대체 실행 비율의 분모로 쓴다. Claude·판가름을 CLI로 옮기면 기존 Workflow 비교군에는 없던 작업이 분모에 들어간다. 모의 입력에서 동일한 외부 실패 2건이 기존에는 2/10으로 미달했지만, Claude 성공 작업을 추가하면 2/24로 통과했다.
   제안: 비교군과 후보군의 대상 역할·호출 모집단을 통일한다. 외부 작업과 Claude 작업의 실패·대체 비율을 별도로 보고하고 각각 상한을 정한다.

5. CLI 모드에서 `CF usage`를 생략한다는 규칙이 혼합 실행 범위와 모순된다.
   위치: docs/2026-10-28-claude-runner-design.md:94
   근거: 94행은 `--claude-via workflow`일 때만 `CF usage`를 쓴다고 하지만, 64·75행은 CLI 모드에서도 Claude 의견·재검토와 그 대체 실행을 Workflow로 남긴다. 현재 `cascade/cli.py:1843`은 Workflow 결과를 저장하면서 해당 Workflow의 사용량을 수집한다.
   제안: 실행 전체의 모드가 아니라 개별 호출 경로로 수집 방식을 결정한다. CLI 작업 뒤 Workflow 검토가 이어지는 경우 양쪽 비용이 보존되는 테스트를 추가한다.

6. 실행 경로를 설정·변경하는 명령 계약이 일관되지 않다.
   위치: docs/2026-10-28-claude-runner-design.md:44
   근거: 44행은 옵션을 `start`·`init`에서 받아 원장에 저장한다고 하지만, 72·135행은 `go --claude-via cli`를 제시한다. 경로 전환은 `jobs --force`로만 한다면서 새 경로 값을 전달하는 방법은 없다. 현재 `cascade/cli.py:2420`의 `jobs` 파서와 783행의 구현에도 경로 변경 계약은 없다.
   제안: 명령별 옵션을 확정한다. 예를 들어 최초 설정은 `start/init`, 변경은 `jobs --force --claude-via …`, `go`는 저장된 값을 사용하도록 정하고 문서·테스트 예시를 통일한다.

Suggestion

1. 기본값 전환 범위를 실제로 검증한 모델·모드와 연결하는 편이 좋다.
   위치: docs/2026-10-28-claude-runner-design.md:155
   근거: 141행의 동등성 비교군은 `local-sonnet-b`이며, 참조 문서 `docs/2026-10-26-cost-effectiveness.md:17`의 표본은 코드 모드 37건이다. 반면 152행의 이전 범위에는 문서 판정자·작성자·점검자도 포함된다. `cascade/jobs.py:84`는 작업자와 점검자에 서로 다른 출력 스키마를 사용한다.
   제안: 기본값 전환을 검증한 범위로 한정하거나, Haiku 및 문서 작성자→점검자 경로의 검증 조건을 추가한다.

2. 확정 사항과 미결 사항을 두 문서에서 동기화하면 구현 범위가 명확해진다.
   위치: docs/2026-10-28-claude-runner-design.md:188, docs/2026-10-28-orchestration-cost-design.md:100
   근거: 러너 문서 21·66행은 별도 `run` 없이 `go`를 확장한다고 확정하지만, 188행은 두 대안을 미결로 남긴다. 비용 문서 100행은 이전의 서브에이전트 맥락 조건을 언급하지만, 107행은 현재 세션 맥락 조건으로 변경했다고 명시한다.
   제안: 현재 결정은 한곳에서 관리하고, 과거 결정은 변경 이력으로 분리한다. 미결 목록에는 실제로 남은 선택만 둔다.

SUMMARY: critical=0 warning=6 suggestion=2
## 11. 구현 결과와 지적 판정 (2026-10-28)

위 「검토에서 남은 지적」을 코드와 대조해 판정하고 (a-full) 1차를 구현했다. 판정은 코드를 읽어 확인한 것이고, 실제 동시 실행·유료 호출로 재현한 것은 없다.

### 11.1 지적 판정

| 지적 | 판정 | 처리 |
| --- | --- | --- |
| W1 작업 id 중복 제거가 재시도 비용과 충돌 | 사실(최초·대체 실행이 같은 작업 id를 쓴다) | CLI 호출 기록은 작업 id로 합치지 않고 시도별 키(`workflows.cli-<stage>-<n>`)에 행을 쌓는다. 다시 돌려도 앞 시도와 실패 호출의 비용이 남는다. 같은 시도 재수집 중복 제거는 필요 없다(러너가 한 번만 쓴다) |
| W2 `jobs --force`가 `workers`와 직렬화되지 않음 | 사실(`cmd_jobs`는 `workers.lock`을 잡지 않았다) | `jobs`가 `workers.lock`을 잡아 `workers`가 도는 동안은 거절한다. 입력 세대 ID로 전환 전 결과를 거부하는 것은 하지 않았다(미해결) |
| W3 평가기가 Workflow 입력 목록만 본다 | 사실(`eval/score.py`의 `claude_jobs`) | `cli_steps.claude_job_entries`를 만들어 `CLAUDE_JOB_FILES`에 `stage1-cli-args.json`·`checker-cli-args.json`을 더하고 보고서·평가기가 같은 함수로 `runner == claude`인 작업을 센다(외부 runner 작업은 세지 않는다). 사용량이 전부 빠진 CLI 실행은 「미측정」으로 나오는 것을 테스트로 확인했다. 판가름 입력(`adj-cli-args.json`)은 이번 범위 밖이라 없다 |
| W4 대체 실행 비율 분모가 경로마다 다름 | 사실로 읽힘(`workers-*` 이벤트 작업 수 합산, 재현 안 함) | 측정 단계(6절) 몫이다. 구현은 바꾸지 않았다. 측정 때 외부 작업과 Claude 작업의 대체 비율을 따로 보고한다 |
| W5 CLI 모드에서 `CF usage` 생략 규칙이 혼합 실행과 모순 | 사실 | 모드가 아니라 호출 경로로 정한다. CLI 호출은 `workers`가 기록하고 Workflow 호출은 `CF usage`가 같은 `claude-usage.json`에 각자의 키로 쓴다(`CF usage`는 자기 Workflow 키만 바꾼다). 두 경로가 섞인 실행의 합산을 통합 테스트로 검증하지는 못했다(미확인) |
| W6 실행 경로 설정·변경 명령 계약 불일치 | 사실 | 확정: 최초 설정은 `start`·`init`의 `--claude-via`, 변경은 `jobs --force --claude-via …`(판정·적용 전), `go`는 원장 값을 쓴다 |
| S1 기본값 전환 범위 | 타당 | 기본값 전환은 측정한 범위(코드 모드 `local-sonnet-b` 표본)로 한정한다. 문서 모드·Haiku 단독은 따로 검증한다 |
| S2 확정·미결 동기화 | 타당 | 9절을 갱신하지 않고 아래 11.2에 현재 결정을 모았다. 9절은 당시 목록으로 둔다 |

### 11.2 이번에 정한 설계 결정

- **진행 명령**(9절 6번): `go` 확장. 별도 `run`은 만들지 않았다. `next_actions`는 바꾸지 않았다: Claude 작업을 `stage1-cli-args.json`으로 보내면 기존 `workers` 행동이 그대로 나온다.
- **경로 확정 시점**(13번): 원장 `claude_via`(`workflow`|`cli`, 옛 원장은 `workflow`). `jobs`가 읽는다.
- **1차 범위**(14번): 1단계 작업자·점검자만 옮긴다. 판가름은 `workers --stage adj` 계약(3절 1번)이 필요해 PR 크기와 검토 범위를 위해 뒤로 미룬다. 따라서 `claude_via cli`여도 판가름·의견·재검토와 Claude 대체 실행은 Workflow가 남고, 이 PR만으로 「Workflow 없이 `report`까지」는 되지 않는다(합의로 끝나는 지적만 있으면 재검토에서 멈춘다).
- **Agent 도구**(8번): 허용하지 않는다(`--disallowedTools`). 비용은 봉투의 `total_cost_usd`를 쓴다(세션 기록을 읽지 않는다). 하위 에이전트가 없으므로 5.2의 누락 위험이 없다.
- **사용량 기록**(15번): 작업별 파일 대신 `workers`가 끝난 뒤 한 곳(`_workers`, `workers.lock` 아래)에서 시도별 키로 쓴다. `workers`가 하나만 돌므로 잠금은 필요 없다.
- **모델 ID**(10번): 러너 안에서 막는다. Claude 결과의 `model_id`는 접두사 없는 요청 모델이고, 봉투의 `modelUsage`에 요청 모델이 하나도 없으면 `무효`다. 전용 종료 코드·`--strict-model`은 두지 않았다.
- **사용자 설정**(5.3): `--setting-sources project --strict-mcp-config`로 사용자 MCP·설정을 끄고 `CLAUDECODE`는 자식 환경에서 지운다. `--bare`는 평가하지 않았다.
- **계획 확인 문구**(5.4): SKILL.md에 한 줄을 더했다. 그 줄이 있을 때만 `--claude-via cli`를 쓴다.

### 11.2.1 판가름·의견 이전 (2차 구현, 2026-10-29)

- **범위**: 판가름(`workers --stage adj`)과 Claude 의견(`workers --stage opinion`)을 `--claude-via cli`로 옮겼다. `adj-cli-args.json`(`role=adjudicator`·`kind=adjudicator`·`writable=true`·판가름 워크트리)과 `opinion-cli-args.json`(`role=opinion`·`kind=review`·`writable=false`·base 워크트리)이 3.1의 규칙대로 메타데이터를 싣는다. `adjudicate`는 `--results`가 선택이고, 없으면 `adj-cli.json`+`adj-fallback.json`을 `load_stage_results` 규칙으로 읽는다. `opinion --results opinion-cli.json`은 기존 Workflow 결과 모양 그대로다. 기본값 `workflow`의 파일·출력은 바뀌지 않는다(HEAD와 파일 단위로 비교해 확인).
- **의견은 base 워크트리에서 도는 것으로 충분하다고 판단했다**: `jobs`가 항상 base 워크트리를 만든다(`cli.py` `_jobs`). 그래서 `_run_isolated`를 써도 사용자 저장소가 건드려지지 않는다. snapshot에 base 워크트리가 없으면 CLI 파일을 만들지 않고 Workflow 경로로 둔다. 의견들이 base 하나를 공유하므로 읽기 역할 검사와 복구가 겹치지 않게 순차로 돌린다(의견 N건이 최대 N×900초).
- **재검토(와 첫 검토, 그 칸의 Claude 대체 실행)는 격리 경로 검증 전까지 Workflow를 유지한다.** 근거: 재검토는 사용자 저장소 루트(`recheck-repo.txt`)를 읽는데, `_run_isolated`가 쓰는 `tree_of`·`restore_tree`는 그 폴더에 `git add -A`·`git clean`을 실행해 모델이 아무것도 쓰지 않아도 사용자의 스테이징 상태를 바꾼다. 5.1이 제안한 전용 경로(인덱스를 쓰지 않는 `git status --porcelain`·diff 해시 비교, 복구 없이 무효 처리)는 실제 `claude -p`로 검증하지 못해 만들지 않았다. 따라서 재검토가 Claude 칸인 프로필(`claude-only` 등)은 여전히 `report`까지 Workflow가 필요하고, 재검토 칸이 Codex·Cursor이면 Workflow 없이 `report`까지 간다. 후속 과제.
- **판가름 프롬프트**: cli 모드에서는 「커밋하지 않는다」 규칙과 충돌하지 않도록 `{commit_step}` 자리표시자로 커밋 지시를 바꾼다(workflow 모드 출력은 바이트 동일).
- **상태 규칙**: 소진(한도·인증) 상태는 의견 단계에서 같은 단계(`workers-opinion`) 기록에서만 물려받고, `--force`면 무시한다(의견에는 Workflow 대체 실행이 없어 다시 돌릴 길이 이것뿐이다). 의견 단계 이벤트는 `fallback=[]`와 `failed` 키를 쓰며, 보고서·평가기는 의견을 대체 실행 비율·무효 결과 비율의 분자·분모에서 뺀다. `judge --force`가 판가름 대상을 없애면 cli 모드에서 낡은 `adj-cli*.json`을 지운다(workflow 모드의 낡은 `adj-args.json`이 남는 기존 동작은 그대로).
- **미구현·후속**: `eval/run_time.py`의 `cli_seconds`가 모든 `workers-*` 이벤트를 마지막 `jobs` 시각부터 재서 새 단계에서 시간을 중복으로 센다(점검자에도 있던 방식). 각 단계의 입력 이벤트부터 재도록 바꾸는 일이 남았다. 판가름·의견은 가짜 실행 파일로만 검증했고 실제 `claude -p` 호출·한도·로그인 문구·`modelUsage` 형식은 미실측이다. 검토는 3회차까지 했고 마지막 반영분(무효 결과 분모, `judge --force` 정리)은 재검토 없이 테스트로만 확인했다.

### 11.3 구현하지 않은 것(미확인·미해결)

- 격리 점검(5.1)과 유료 측정(6절 1~3단계): 하지 않았다. 도구 허용 목록과 사후 검사(`_run_isolated`)까지만 있고 작업 폴더 밖 쓰기를 막지 못한다. 격리가 Workflow 수준임을 보이기 전에는 기본값을 `cli`로 바꾸지 않는다.
- `claude -p`의 한도·로그인 문구, `modelUsage` 키 형식: 미실측. 가짜 실행 파일로만 검증했다. `classify_failure`에 `run /login`·`invalid api key`를 더했다(일반 `/login`은 Codex·Cursor의 경로 문구 `app/login/views.py`와 겹쳐 좁혔다). 로그인 안내(`LOGIN_COMMANDS`)에 claude를 넣었다.
- 한도 정지(5.5): 미구현. 러너의 `_Exhaustion`이 같은 `workers` 실행 안의 나머지 Claude 호출을 건너뛰게 할 뿐, 한도에 걸린 뒤 `go`가 멈추거나 재개 시점을 알려 주지 않는다. 한도 문구는 미실측이다.
- 대체 실행 사용량: 실패한 CLI 시도의 행과 Workflow 대체 실행의 행은 같은 작업 id라, CLI 기록 행에 `via: cli`를 달고 보고서(`_ClaudeUsage`)·평가기(`collect_usage`)가 (작업 id, 실행 경로)별로 센다. `workers --force`로 CLI 시도가 여러 번 쌓여도 Workflow 대체 실행분의 기록을 대신하지 못한다.
- 재개·`--retry-failed`·부모 강제 종료 뒤 자식 정리(5.5): 하지 않았다. 한도·로그인 실패 뒤에는 기존처럼 `jobs --force`로 회차를 다시 연다. Claude 작업이 실패하면 그 작업은 Claude 대체 실행 Workflow(`*-fallback-args.json`)로 간다.
- W2의 입력 세대 ID, W4의 분모 통일.
