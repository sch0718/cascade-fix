# 최종 브랜치 검토 지적 목록 (2026-10-17, 범위 965c3b6..6c9c6d8)

판정: 수정 뒤 가능. 아래 Critical 5·Important 8·Minor 10을 한 번의 수정 파동으로 처리한다. 각 항목 끝의 「결정」은 컨트롤러가 정한 것이다.

## Critical

### C1. 포매터가 워크트리 전체를 고쳐 무관한 파일 변경이 모든 패치에 섞인다
- `cascade/patches.py:45-50`, `cascade/verify.py:150-152`, `cascade/config.py:82`(`ruff format .`), `repos.toml` 예시
- `extract`가 워크트리 전체에 `cfg.format`을 돌린 뒤 `diff --cached base`를 뜬다. 저장소가 포매터 기준으로 깨끗하지 않으면 손대지 않은 파일의 포맷 변경이 패치에 들어가고, 첫 채택 패치가 그걸 사용자 트리에 적용하며 뒤 패치는 전부 적용 실패한다(리뷰어 재현: 두 패치 모두 three.py 포함, f2 실패, 사용자 트리 three.py 변경).
- 결정: 먼저 `git diff --name-only base` + 미추적 파일로 작업자가 바꾼 파일을 구하고, 포매터는 그 파일에만 돌린다(`format` 명령에 `{files}` 자리표시자, 없으면 파일 인자를 뒤에 붙임; `detect_candidates`도 `ruff format {files}`), diff도 `-- <그 파일들>`로 제한한다. 손대지 않은 미포맷 파일이 있는 픽스처로 테스트.

### C2. 작업자가 워크트리를 벗어나도 잡는 장치가 없다
- `cascade/jobs.py:105-114`, `:20-38`, `prompts/worker-code.md:16-24`, `workflows/run-jobs.js`
- Workflow 에이전트의 현재 디렉터리는 사용자 저장소 루트다. 프롬프트의 「위치: calc.py:2」에 워크트리 접두가 없어 Haiku가 사용자 루트의 파일을 고칠 수 있고(패치는 비고 사용자 파일만 바뀜), `git stash`/`stash pop`으로 사용자 stash를 꺼내 cleanup이 지울 수 있다.
- 결정: ① 프롬프트의 위치를 `{worktree}/<path>:<line>` 절대 경로로 렌더하고 「현재 디렉터리는 사용자 저장소다. 반드시 워크트리 절대 경로만 쓴다」를 넣는다. ② RULES·READ_ONLY_RULES에 「`git stash`·`checkout`·`switch`·`reset`·`branch`·`push` 금지, 수정 전 상태는 `git show HEAD:<파일>`로 본다」를 넣는다. ③ 기계 경계: `jobs`에서 사용자 트리 지문(별도 임시 인덱스로 `write-tree`한 트리 해시, HEAD, `refs/stash`, `for-each-ref refs/heads`)을 원장에 적고, `judge`·`adjudicate` 시작 때 다시 재서 다르면 종료 코드 6으로 멈추고 `git diff --stat <트리1> <트리2>`를 보인다. 테스트: jobs 뒤 사용자 파일을 고치면 judge가 6으로 멈춤.

### C3. 문서 모드: 저장소 밖·git 제외 문서의 위치가 워크트리 경로로 바뀌지 않는다
- `cascade/cli.py:77-92`, `cascade/jobs.py:163-171`
- Codex는 원본 경로(`docs/superpowers/plans/x.md:690` 또는 절대 경로)로 위치를 적는데 워크트리 사본은 `외부/x.md`에만 있어 작성자가 원본을 직접 고친다. e2e 테스트가 위치를 「외부/plan.md」로 넣어 이를 가린다.
- 결정: `jobs`(문서 모드)에서 `ledger.docs`를 거꾸로 써 원본 경로(상대·절대)를 워크트리 키로 바꾼다. 프롬프트에 「대상 문서: `{worktree}/외부/x.md`」와 「원본 경로는 열지 않는다」를 넣는다. 테스트의 위치를 Codex가 실제로 쓰는 형식(원본 상대 경로)으로 고친다.

### C4. 문서 모드 참조 실존 검사가 문서 전체를 봐서 실제 문서에서는 늘 실패한다
- `cascade/cli_steps.py:211-222, 229-231`, `cascade/verify.py:163-180`, `cascade/consensus.py:138-139, 168-169`
- 리뷰어 재현: 이 스킬의 계획서를 검사하면 「없음」 42건, SKILL.md도 20건. 판정이 늘 「판가름(참조 없음)」으로 가고 Opus 패치도 실패해 문서 모드는 아무것도 채택하지 못한다.
- 결정: 기준 문서(스냅샷)와 패치 뒤 문서 양쪽에서 검사해 차집합(새로 생긴 깨진 참조)만 문제로 본다. `~`, `<`, `$`, `{`가 든 토큰은 건너뛴다. 테스트: 기존에 깨진 참조가 있는 문서에서 수정이 새 참조를 더하지 않으면 `refs_missing`이 빈 목록.

### C5. PR 코멘트의 재현 명령을 확인 없이 `shell=True`로 실행한다
- `cascade/verify.py:220-246`, `cascade/cli.py:225-229, 324-325`
- 코멘트를 달 수 있는 누구든 펜스에 넣은 명령이 사용자 셸 권한으로 두 번 실행된다. `$ ` 없는 출력 줄(`FAILED tests/...`)도 명령으로 실행된다.
- 결정: `jobs`(pr-comment)가 뽑은 재현 명령을 `round-N/repro-cmds.json`에 쓰고 출력한다. `judge`는 `--allow-repro <파일>`(사용자가 승인한 명령 목록, 내용이 `repro-cmds.json`과 같아야 함)이 없으면 재현 검증을 건너뛰고 경고한다(멈추지 않는다 — 재현 명령은 보조 검증기이므로). 펜스 안에 `$ ` 줄이 하나라도 있으면 그 줄만 명령으로 받는다. SKILL.md에 「사용자에게 명령 목록을 보이고 승인받은 뒤 `--allow-repro`로 넘긴다」를 적는다.

## Important

### I1. 기준선 검증이 없고 워크트리에 의존성이 없다
- `cascade/snapshot.py:106-120`, `cascade/verify.py:137-147`, `cascade/cli.py:397-405, 438-442`
- 워크트리에는 `node_modules`·`.venv`가 없고, 이미 깨진 린트·테스트가 있으면 A·B 검증이 함께 실패해 전부 Opus로 간다. 기존에 실패하던 전체 테스트는 「간섭 의심」으로 오판된다.
- 결정: `repos.toml`에 `setup: list[str]`(워크트리마다 한 번 돌리는 명령, 예: `ln -s <root>/node_modules node_modules`, `{root}` 자리표시자)을 둔다. `jobs`에서 `base` 워크트리에 setup을 돌린 뒤 worker 단계 검증기를 한 번 돌려 실패한 명령을 `round-N/baseline.json`에 적는다. `judge`는 기준선에서 실패한 명령을 판정에서 제외하고(결과에는 「기준선 실패」로 남김) 보고서에 알린다. `apply` 전에 전체 테스트를 기준선(스냅샷 base 워크트리)에서 한 번 돌려 이미 실패하면 「간섭 의심」 대신 「기준선부터 실패」로 적고 채택분을 판단불가로 보내지 않는다.

### I2. 오래 걸리는 명령이 Bash 시간 제한에 잘리고, apply는 다시 돌리면 안전하지 않다
- `SKILL.md:64-76`, `cascade/cli.py:425-462`, `cascade/verify.py:13`
- 결정: SKILL.md에 `judge`·`adjudicate`·`apply`를 `run_in_background`로 돌리고 완료 알림을 기다리라고 적는다. `apply_all` 직후 `round-N/apply.json`을 쓰고 원장에 `apply-patched` 이벤트를 저장한다. 다시 돌릴 때 그 이벤트가 있으면 적용을 건너뛰고 뒷단계만 한다. 두 판정이 모두 비사실이고 패치가 비었으면 검증기를 생략한다.

### I3. 회차 결과 커밋에 테스트 산출물과 사용자 동시 편집이 섞인다
- `cascade/cli.py:445, 451-452`, `cascade/cli_steps.py:289-294`, run-review.sh argv 전달
- 결정: `snapshot_tree`는 `apply_all` 직후, 전체 테스트 전에 찍는다. 재검토 diff는 `git diff base result -- <적용 패치의 파일들>`로 제한한다. run-review.sh의 stdin 전달은 `codex exec --help`에서 `-`(stdin) 지원이 확인될 때만 바꾸고, 아니면 argv 그대로 두되 diff가 500KB를 넘으면 재검토 프롬프트에서 diff를 생략하고 파일 경로 목록만 넣고 「diff는 `git diff <base> <result>`로 본다」고 적는다.

### I4. dry-run 2회차부터 기준이 틀리다
- `cascade/cli.py:425-428, 449-454, 516`
- 결정: dry-run에서는 기준 커밋에서 `result-rN` 워크트리를 만들어 거기에 `apply_all`로 채택 패치를 적용하고 `commit_worktree`로 커밋해 `result_commit`으로 삼는다. 재검토 `--repo`도 그 워크트리를 가리키도록 `recheck-prompt.md` 옆에 `recheck-repo.txt`를 쓰고 SKILL.md가 그 값을 쓴다.

### I5. 재검토의 「범위 밖」 지적을 따로 모으지 않는다(설계 10절 2)
- `cascade/cli.py:486-499`, `cascade/rounds.py:105-119`
- 결정: 위치 파일이 재검토 diff의 `changed_files`에 없으면 「범위 밖」으로 분류해 `보류후보`(사유 `재검토 범위 밖`)로 보내고 회차를 열지 않는다. `classify_recheck`에 `in_scope: set[str] | None` 인자를 더한다.

### I6. Workflow args를 컨트롤러 모델이 통째로 옮겨 적는다
- `cascade/jobs.py:135-144`, `SKILL.md:65`
- 결정: 프롬프트는 `round-N/prompts/<id>-<role>.md`에 쓰고, `args.jobs`에는 `{id, label, model, effort, kind, promptPath}`만 둔다. `run-jobs.js`가 `kind`(`worker|checker|adjudicator`)별 스키마를 갖고, 프롬프트는 「다음 파일을 Read로 읽고 그 지시를 따른다: <promptPath>」 한 줄로 만든다. Task 1의 형태 테스트와 jobs 테스트를 맞춘다.

### I7. 워크트리가 사용자 저장소 안(`.cascade-fix/`)에 있어 jest·vitest·eslint가 사본을 함께 돈다
- 결정: 위치는 그대로 둔다(`.git/` 아래는 Claude Code가 에이전트 쓰기를 막는다). 대신 `apply`에서 전체 테스트를 돌리기 전에 그 회차의 작업자·판가름 워크트리를 제거한다(`base`는 기준선용으로 남김). `repos.toml` 예시와 SKILL.md에 「jest·vitest·eslint는 `.cascade-fix`를 제외 설정에 넣으라」 한 줄.

### I8. 보안 경로 판정이 루트 경로와 대소문자를 놓친다
- `cascade/config.py:7`, `cascade/cli_steps.py:198-199`
- 리뷰어 재현: `auth.py`, `auth/handler.go`, `migrations/0001.sql`, `src/AuthService.ts` 모두 False.
- 결정: `path`와 `"/"+path`를 모두 소문자로 바꿔 대조한다. 테스트 추가(위 경로 전부 True, `src/author.py`는 패턴에 따라).

## Minor (첫 사용 전에 고침)

1. `cascade/rounds.py:159-167` — 해소 여부 줄의 id에 `**`·백틱이 붙으면 못 찾음 → `*`과 백틱을 벗기고 `r\d+-\d+`로 찾는다.
2. `cascade/cli_steps.py:345-350` — resume 회차가 끝나도 `report.md`가 있어 report를 건너뜀 → 원장에 `report` 이벤트(회차 번호 포함)를 적고 마지막 회차 이후 했는지로 판단한다(이벤트는 시간 집계에서 제외).
3. `cascade/config.py:39-40` — `tests = "pytest"` 문자열이면 글자별 실행 → 목록 필드가 목록이 아니면 종료 코드 5.
4. `cascade/verify.py:17, 71-75`, `cascade/cli_steps.py:305-310` — 대체 경로가 `.venv`까지 훑음 → 파일 열거는 `git ls-files -co --exclude-standard`로.
5. `cascade/report.py:244-276, 377-388` — dry-run인데 답글 초안이 「반영했습니다」, resolve 대상 채워짐 → dry-run이면 답글·resolve를 쓰지 않고 「적용 안 됨(dry-run)」만 적는다.
6. `cascade/cli.py:123-138` — PR 모드에서 로컬 HEAD가 PR head인지 미확인 → `gh pr view <n> --json headRefOid`와 대조해 다르면 경고.
7. `prompts/opinion.md:28-36`, `cascade/rounds.py:172-183` — 문서 모드에서 작성자 패치를 「a의 패치」로 표시 → 「작성자 패치」, b는 「판정만 함」.
8. `SKILL.md` — 중단할 때 `cleanup` 안내 없음 → 「어느 단계에서든 멈추면 `cleanup`을 돌린다」 추가.
9. `cascade/jobs.py:73, 126` — `<바꾼 코드와 관련된 테스트 파일>` 자리표시자를 글자 그대로 실행할 수 있음 → `jobs` 때 지적 위치 파일로 `related_tests`를 계산해 실제 목록을 넣는다.
10. `eval/replay.py:117` — 표본 저장소 `worktree add`에 훅 차단 없음 → `-c core.hooksPath=<빈 폴더>`.

## 리뷰어가 올리지 않은 미룬 항목
원장의 나머지 `minor (deferred)`는 리뷰어가 「미뤄도 됨」으로 분류했다. 이번 파동에서 손대지 않는다.
