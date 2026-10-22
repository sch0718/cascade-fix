# Cursor 읽기 역할 ACP 상주 실행기 설계 (2026-10-21)

## 왜

`CursorRunner`는 호출마다 임시 HOME에서 `cursor-agent -p`를 띄운다. 짧은 프롬프트도 한 건에 75~180초가 걸린다. 모델 응답은 3~5초이고 나머지는 시작 단계다. 시작 단계에서 Cursor 팀 마켓플레이스 플러그인의 MCP 서버(`uvx … serena`, `npx typescript-language-server`)를 새 캐시에 설치해 띄운다. `cursor-agent acp`(Agent Client Protocol 상주 서버)는 팀 플러그인을 싣지 않아 서버 시작 1초, 세션 8초, 짧은 답 수 초다.

## 실측으로 정한 범위 (cursor-agent 2026.10.16-e3cb207)

- ACP에서 셸은 `session/request_permission`으로 오고, 허락하면 **샌드박스 없이** 돈다. cli-config의 `sandbox.mode=enabled`와 전역 `--sandbox enabled`를 줘도 워크트리 밖 쓰기, 다른 워크트리 쓰기, `curl https://example.com`, 워크트리 밖 파일 `cat`이 모두 성공했다. Write 도구도 권한 요청 없이 작업 폴더 밖에 쓴다.
- 지금 `-p --sandbox enabled`는 네트워크를 막는다(프록시 403). `$TMPDIR` 쓰기와 시스템 전체 읽기는 허용한다.
- Read 거부, MCP 거부, WebFetch 거절은 ACP에서도 막힌다(regmap 조사와 같다).

그래서 **쓰기 역할(코드 작업자 a·b, 문서 작성자)은 지금처럼 `-p`로 둔다.** 셸·쓰기가 필요 없는 **읽기 역할(문서 판정자 a·b, 문서 점검자: `writable=False`)만 ACP로 옮긴다.** 코드 모드의 Cursor 작업(작업자 B)은 빨라지지 않는다.

## 사용자가 정한 것

1. 읽기 역할만 ACP로 옮긴다.
2. 읽기 역할은 ACP에서 셸을 쓰지 못한다(Read·Grep·Glob만). 프롬프트는 바꾸지 않는다.
3. ACP 작업은 토큰 사용량이 없다. `usage_totals`가 사용량 없는 작업 건수를 세고 보고서 사용량 표에 「기록 없음」 건수를 보인다. `eval/score.py`는 건드리지 않는다.
4. `docs/cursor-review/run-review.sh`(검토자)와 `cascade/sandbox_check.py`(쓰기 역할 샌드박스 사전 점검)는 그대로 `-p`다.

## 구조

### `cascade/cursor_acp.py` (새 파일)

regmap `tools/regmap/cursor_acp.py`의 `_Server`·풀을 cascade 형태로 옮긴다.

- `_Server`: 임시 폴더(`cascade-cursor-acp-*`)에 HOME을 만들고(`cursor_home(base, "review")`: Shell·Write·WebFetch·MCP 거부, 홈 민감 경로 Read 거부, `approvalMode=allowlist`) `cursor-agent acp`를 새 프로세스 그룹으로 띄운다. env는 지금 `-p`와 같은 규칙(`_tool_roots_env`, `cache_env(임시 폴더)`, `HOME`)이다.
  - `initialize`: `fs.readTextFile/writeTextFile=false`, `terminal=false`, `_meta.parameterizedModelPicker=true`.
  - 서버가 보내는 `session/request_permission`은 모두 거절(`reject_once`, 없으면 `cancelled`). 다른 클라이언트 요청은 JSON-RPC 오류로 답한다.
  - 닫기: stdin 닫기 → 프로세스 그룹 SIGTERM → 5초 뒤 SIGKILL → 그룹이 사라질 때까지 대기(최대 3초) → 임시 폴더 삭제(2.5초 동안 재시도). `atexit`로 남은 서버를 닫는다.
- `CursorAcpPool(exe, max_servers=MAX_PARALLEL, sessions_per_server=50, timeout=JOB_TIMEOUT, start_timeout=180)`
  - 쉬는 서버를 빌려 주고, 없으면 `max_servers`까지 새로 띄우고, 넘으면 기다린다. 성공한 서버는 세션 수가 50 미만이면 돌려놓고, 실패했거나 50에 닿으면 닫는다.
  - `ask(worktree, model, prompt, retry_prompt, accept) -> (text, detail)`:
    작업마다 `session/new`(cwd=작업 워크트리, `mcpServers=[]`) → `session/set_config_option`으로 `model=<작업 모델>`, `fast=false`, `mode=ask` → 실제 값이 다르면 프롬프트를 보내지 않고 오류. 모델 목록에 없거나 `auto`로 시작하면 오류.
    `session/prompt` 응답의 `agent_message_chunk`를 모은다(도구 호출이 오면 앞 문장을 버린다). `stopReason`이 `end_turn`이 아니면 오류. 결과가 스키마에 맞지 않으면 **같은 세션**에 재요청 문장을 두 번째 prompt로 보낸다.
  - 시간 제한: 서버 시작은 `start_timeout`, 작업(세션 열기~재요청)은 합쳐서 `JOB_TIMEOUT`(15분).
  - `close()`: 모든 서버를 닫고 기다리는 호출을 깨운다.

### `cascade/runners.py`

- `CursorRunner.run(job)`: `job.writable`이면 지금 `-p` 경로 그대로. 아니면 풀(처음 쓸 때 만든다)로 보낸다. 출력 검사·재요청·`model_id` 덮어쓰기는 지금 `_check_output`·`_RETRY_PROMPT`를 그대로 쓴다.
- 상태 분류는 지금과 같은 상태값을 쓴다: JSON-RPC 오류 메시지와 서버 stderr(프롬프트 원문 제거)를 `classify_failure`로 한도·로그인·오류로 나눈다. 시간 초과는 `timeout`, 서버 종료·프로토콜 오류는 `error`, 재요청 뒤에도 스키마가 맞지 않으면 `invalid`.
- ACP 결과의 `usage`는 `{"unrecorded": 1}`이다.
- `CursorRunner.close()`를 더한다(풀이 있으면 닫는다).
- `_run_isolated`의 읽기 역할 변경 되돌리기, 한도·로그인 뒤 건너뛰기, 대체 실행(Claude)은 그대로다.

### 연결부

- `cascade/cli.py` `cmd_workers`: `run_all` 뒤 `finally`에서 실행기마다 `close`가 있으면 부른다(판정 로직 변경 없음).
- `cascade/workers.py` `usage_totals`: 실행기별 합계에 `unrecorded`(사용량 없는 작업 건수)를 더한다.
- `cascade/report.py` 「외부 작업자」 사용량 표: 「기록 없음(건)」 열을 더한다.

### 격리 점검 `cascade/cursor_acp_check.py` (새 파일)

regmap `cursor_acp_check.py`와 같은 방식으로, cascade의 ACP 프로필(읽기 역할)로 짧은 시험 문장만 보낸다.

- 사례: Read(워크트리 밖 카나리아), 셸 `cat`, 워크트리 안 쓰기, 워크트리 밖 쓰기, Grep·Glob, WebFetch, MCP(시험용 MCP 서버를 `session/new`에 붙임).
- 판정은 답 텍스트, 세션 저장소(`acp-sessions/*/store.db`), 파일 생성, MCP `tools/call` 수신으로 한다(진행 알림의 `completed`는 믿지 않는다).
- 카나리아는 임시 폴더에 두고, 점검용으로 그 폴더 Read 거부를 덧붙인다(실제 프로필은 홈 민감 경로를 같은 규칙으로 막는다).
- `--control`: 거부 설정을 비우고 Read만 돌려 시험이 누출을 잡는지 확인한다.
- 실행: `python3 -m cascade.cursor_acp_check [--control]`. 종료 코드: 누출 없음 0, 누출·오류 1.
- 문서에 「Cursor CLI가 바뀌면 다시 돌린다」고 적는다.

## 시험

- `tests/fake_cli.py`의 가짜 `cursor-agent`에 `acp` 하위 명령(JSON-RPC 줄 단위 서버)을 더한다. regmap `fake_model.py`처럼 환경 변수로 오류·한도·멈춤·권한 요청·도구 호출·종료 사유·설정 불일치를 흉내 내고, 받은 요청을 로그에 남긴다.
- `tests/test_cursor_acp.py`: 읽기 작업이 ACP로 가는지(initialize 인자, session/new cwd, 설정 세 가지, 거부 설정 파일), 쓰기 작업은 `-p`로 가는지, 서버 재사용·동시 한도·50세션 교체, 권한 거절·클라이언트 요청 오류, 같은 세션 재요청, 한도·로그인·시간 초과·서버 종료 뒤 회복, 설정 불일치·모델 없음·Auto 거절 시 프롬프트 미전송, 닫은 뒤 프로세스 그룹·임시 폴더 정리, `usage_totals`·보고서의 기록 없음.
- `tests/test_cursor_acp_check.py`: 판정 함수(누출·막힘)와 `--control` 종료 코드.
- 전체 시험, `claude plugin validate .`.

## 실측(구현 뒤)

- 격리 점검: Read·셸·쓰기·검색·Web·MCP 6건 모두 막힘. `--control`은 카나리아를 읽어 대조 성공.
- 속도: 짧은 시험 프롬프트로 읽기 작업 8건을 동시 4로 돌려 26초(서버마다 첫 작업 16~18초, 이후 5~7초). 남은 프로세스·임시 폴더 없음.

## 하지 않는 것

- 쓰기 역할의 ACP 전환, `-p` 시작 지연(팀 플러그인 MCP) 해결, `-p` 종료 뒤 남는 하위 프로세스 정리, 검토자(`run-review.sh`)·`sandbox_check.py` 전환, `eval/score.py` 변경.

## 구현자가 정한 것

- ACP 프로필은 새로 만들지 않고 `cursor_home(…, "review")`(Shell·Write·Web·MCP 거부)를 쓴다.
- 세션 모드는 `ask`다(regmap과 같다. 거부 설정과 이중으로 막는다).
- ACP를 끄는 환경 변수는 두지 않는다(요청 범위 밖).
