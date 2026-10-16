# SDD ledger — plan: /Users/sch0718/01.workspace/pr-auto-review/docs/2026-10-17-model-cascade-fix-skill-plan.md
spec: /Users/sch0718/01.workspace/pr-auto-review/docs/2026-10-17-model-cascade-fix-skill-design.md
repo: /Users/sch0718/.claude/skills/cascade-fix (branch feature/cascade-fix, 로컬 전용, 원격 없음)

## 사전 점검 (2026-10-17)

| 쌍/태스크 | 주는 것 / 받는 것 | 결과 |
| --- | --- | --- |
| T2→T5 | RepoConfig 필드 | 일치 |
| T3→T8 | Finding 필드 | 일치 |
| T4→T6 | Snapshot.commit, extract(wt, snap_commit) | 일치 |
| T4→T11 | commit_worktree, add_worktree("base") | 일치 |
| T5→T7 | VerifyResult(cmd, ok, tail) | 일치 |
| T8→T7/T9 | WorkerResult, AdjInput | 일치 (AdjInput은 T8이 정의, T9가 씀) |
| T1→T8 | Workflow args jobs[id,label,model,effort,prompt,schema] / results[id,output] | 일치 |
| T9→T11 | Ledger, classify_recheck(final=), resume | 일치 |
| T10→T11 | render(ledger, dry_run), write_prepared | 일치 |
| T2↔T11 | exit 4 테스트 @skip → T11에서 해제 | 일치 |
| T4 자체 | 테스트가 "spec.md"를 ls-tree 목록에서 찾지만 경로는 "외부/spec.md" | 불일치 → 디스패치에서 바로잡음 |
| T11 자체 | 픽스처 결과 JSON에 워크트리 경로가 들어가야 하나 경로는 실행 때 정해짐 | 디스패치에서 "테스트가 실행 때 결과 JSON을 만든다"로 명시 |
| 전역 제약 "커밋 단계 없다" ↔ sdd 절차(커밋·diff 리뷰) | — | Ruling 1 |
| T9 run-review.sh는 이 저장소 밖 | — | Ruling 2 |
| T1 Step 3 Workflow 호출 | 서브에이전트가 아니라 컨트롤러가 돌린다 | Ruling 3 |

- Ruling 1: 스킬 폴더를 로컬 git 저장소(main → feature/cascade-fix)로 만들어 태스크마다 커밋한다 — 리뷰 패키지와 복구에 git 이력이 필요하다 — 틀리면 사용자가 `.git`을 지우면 된다. 원격·push 없음.
- Ruling 2: T9의 run-review.sh 수정은 구현자가 수정 전 사본을 워크스페이스에 두고 `diff -u`를 보고서에 붙이며, 리뷰어는 그 diff를 읽는다 — 저장소 밖 파일이라 review-package가 못 담는다 — 틀리면 리뷰 누락.
- Ruling 3: T1의 Workflow 실측은 컨트롤러가 직접 돌리고 결과를 구현자에게 넘긴다 — 사용자가 계획(실측 포함)을 승인했고 Workflow는 컨트롤러 도구다 — 틀리면 실측 재실행.
- Ruling 4: T4 테스트의 ls-tree 확인은 "외부/spec.md"로 — 계획의 경로 규칙(외부/)과 테스트 문구가 어긋남 — 비용 없음.
- Ruling 5: Workflow 도구는 `scriptPath`를 작업 디렉터리(또는 추가한 디렉터리) 안의 파일만 받는다. `~/.claude/skills/cascade-fix/workflows/run-jobs.js`는 거부됐다(실측 2026-10-17). SKILL.md는 스크립트 파일을 읽어 `script` 입력으로 통째로 넘기게 적는다 — 틀리면 호출 실패로 바로 드러난다. T11 디스패치에 전달.
Task 1: 실측 통과 (Haiku 5.5·Sonnet 5.5·low 확인, 문서 ~/01.workspace/pr-auto-review/docs/2026-10-17-haiku-55-check.md)
Task 1: minor (deferred): 금지어 검사가 주석까지 포함 — 주석에 Date.now를 적으면 오탐
Task 1: minor (deferred): args undefined면 TypeError — 인터페이스상 항상 주어짐
Task 1: complete (commits 965c3b6..3d73ccf, review clean; ⚠️ 순서 보존·null 반환은 실측 문서로 확인)
- Ruling 6: 저장소별 설정 파일 경로는 환경 변수 CASCADE_REPOS_TOML(기본 <스킬>/repos.toml)로 — T2가 임시로 잡은 이름을 T11이 그대로 쓴다 — 틀려도 이름 하나 바꾸는 일.
Task 2: minor (deferred): config.py LIST_KEYS 미사용 별칭; test_config.py import os 미사용
Task 2: minor (deferred): 설정값 타입 미검증(tests="pytest" 문자열), TOMLDecodeError는 CLI(T11)에서 코드 5로
Task 2: minor (deferred): helpers.make_repo는 commit.gpgsign=false 를 주는 게 안전
Task 2: complete (commits 3d73ccf..98b4d52, review clean)
- Ruling 7: parse_codex_code는 항목 0건이면 빈 목록(ParseError 아님) — Codex 코드 리뷰는 지적 없음을 자유 문장으로 적어 구분 불가 — T11 init이 0건일 때 원문 첫 줄들을 보고에 보여 사람이 확인하게 한다.
Task 3: minor (deferred): 코드 위치에 공백 있으면 항목 버려짐 → 원문에 "[P"가 있는데 0건이면 T11 init이 경고
Task 3: minor (deferred): 문서 항목 블록은 들여쓴 줄만; PR 심각도 첫 줄 부분 일치 오탐; subprocess timeout 없음; 페이지네이션 없음(first:100)
Task 3: minor (deferred): 픽스처 로그인 sch0718 → 가짜 ID로
Task 3: complete (commits 98b4d52..202586d, review clean)
- Ruling 8: 임시 커밋(commit_worktree)은 --no-verify — 사용자 훅이 임시 커밋을 막거나 파일을 바꾸면 안 된다 — 훅이 잡던 문제는 검증기(T5)가 대신 잡는다.
Task 4: review (52cfece) Important 2 — 훅(prepare-commit-msg·post-commit·post-checkout) 미차단, 실패한 worktree add·prune 순서로 워크트리 잔존. Minor: run_id 검증, GIT_* env, HEAD 단일 해석, 최선 정리, exclude 비UTF-8
Task 4: fix round 1/5 dispatched (구현자 재개)
Task 4: fix round 1/5 (3 addressed, 0 open; commits 52cfece..01372ae)
Task 4: minor (deferred): commit_worktree가 wt.parent.parent를 run_dir로 가정; cleanup 두 번 호출 시 no-hooks 재생성
Task 4: complete (commits 202586d..01372ae, review clean after 1 fix round)
- Ruling 9: repro_cmds 단락은 「확인한 내용」부터 「근거」·답글·본문 끝까지, 빈 줄은 종료 아님 — PR 코멘트 틀에서 펜스 앞에 빈 줄이 흔하다 — 틀리면 재현 명령을 못 찾아 검증기 하나가 빠진다(합의 규칙엔 영향 없음).
Task 5: review (b25be42) Important 2 — Popen errors 미지정(UnicodeDecodeError), from pkg import mod 미탐지. Minor: 절대경로 검사, http 접두사, tests/ spec, shlex.quote, stdin
Task 5: minor (deferred): 테스트 파일 반복 읽기, SKIP_DIRS에 .venv/dist 없음, repro 주석 줄, tail 2000자·run_repro 시간 초과 테스트 없음, check_snippets cmd에 [블록 n] 접두사
Task 5: fix round 1/5 dispatched (구현자 재개)
Task 5: fix round 1/5 (3 addressed, 0 open; commits b25be42..59012d8)
Task 5: minor (deferred): 괄호 여러 줄 import 미탐지, 주석 단어 과매칭, http 접두사 테스트 공백
Task 5: complete (commits 01372ae..59012d8, review clean after 1 fix round)
Task 6: review (28799dc) Important 2 — git diff가 사용자 설정(noprefix·renames·textconv)에 의존, text=True로 CRLF 손실. Minor: changed_files hunk 본문 오인, 실패 경로 인덱스 불변 테스트 없음
Task 6: minor (deferred): normalize("")=="\n", 탭 포함 경로 잘림, 공백만 있는 문맥 줄은 비교 전용
Task 6: fix round 1/5 dispatched (구현자 재개)
Task 6: fix round 1/5 (3 addressed, 0 open; commits 28799dc..80d065d)
Task 6: minor (deferred): 비UTF-8 텍스트는 U+FFFD로 바뀜, 따옴표 경로 건너뜀, diff.algorithm 등이 same() 비교에 영향 가능
Task 6: complete (commits 59012d8..80d065d, review clean after 1 fix round)
- Ruling 10: decide_doc에서 checker가 None이면 판가름(점검자 없음) — 점검 없이 채택하지 않는 쪽이 설계 4절 반목표에 맞다 — 비용: 문서 모드 Opus 호출이 조금 늘 수 있음(T8은 항상 점검자를 보낸다).
Task 7: review (2421f24) Important 1 — after_adjudication이 보안 경로 결정을 채택 가능. Minor: verdict 값 미검증, checker_ok None 문구, 테스트 공백
Task 7: minor (deferred): after_adjudication 설계결정+검증 실패 동시 우선순위 테스트 없음; 문서 모드 patch_from="adjudicator" 의미는 T8이 정함
- Ruling 11: 보안 경로 결정은 판가름 뒤에도 채택하지 않는다(판단불가) — 설계 13절 — 비용 없음.
Task 7: fix round 1/5 dispatched (구현자 재개)
Task 7: fix round 1/5 (2 addressed, 0 open; commits 2421f24..066c2c3)
Task 7: complete (commits 80d065d..066c2c3, review clean after 1 fix round)
Task 8: review (e7e9eeb) Important 2 — 판가름 프롬프트에 검증 명령 없음, 문서 모드 작성자·점검자 구조 미반영. Minor: 무조건 커밋 규칙, verify tail 누락, 구분자 충돌(드묾)
- Ruling 12: AdjInput에 cfg·checker·escalation_reasons를 더한다 — 판가름이 검증 명령과 올라온 이유를 알아야 한다(설계 8.2 「두 결과 전체」) — T11이 이 셋을 채워야 함.
Task 8: minor (deferred): <<<지적 본문 끝>>> 구분자·네 백틱 펜스 충돌 가능(드묾)
Task 8: fix round 1/5 dispatched (구현자 재개)
Task 8: fix round 1/5 (3 addressed, 0 open; commits e7e9eeb..b0721f3)
Task 8: minor (deferred): AdjInput.cfg 기본 None이면 검증 명령 "(없음)" — T11 호출부가 반드시 cfg 전달; 문서 모드 reason 접두 목록에 a/b 패치 채택 잔존
Task 8: complete (commits 066c2c3..b0721f3, review clean after 1 fix round)
Task 9: review (6ceb998) Important 1 — run-review.sh 새 모드가 빈/읽기 불가 프롬프트로 실행됨. Minor: --repo 없음 조용히 통과, Ledger.save 비원자적, findings 키 KeyError, parse_recheck 해소 절 없어도 빈 dict
Task 9: minor (deferred): parse_recheck 해소 절 없으면 빈 dict — T11 recheck 명령이 비면 경고; argv로 넘기는 대용량 diff 한도(ARG_MAX)
Task 9: fix round 1/5 dispatched (구현자 재개)
Task 9: fix round 1/5 (2 addressed, 0 open; commits 6ceb998..56a3838)
Task 9: minor (deferred): 프롬프트가 -로 시작하면 codex 옵션 오인 가능(-- 구분자), Ledger .tmp 이름 고정
Task 9: complete (commits b0721f3..56a3838, review clean after 1 fix round; run-review.sh 변경은 task-9-run-review.diff)
- Ruling 13: 커밋 초안은 decisions 기준이라 적용 실패 건도 오른다 → T11 apply가 실패한 건의 decision을 판단불가로 바꿔 저장해 초안에서 빠지게 한다 — 초안과 실제 트리가 어긋나면 안 된다.
Task 10: review (9dd2356) Important 1 — 준비됨 산출물(resolve·답글·커밋 초안)이 apply.json 실패를 반영하지 않음. Minor: failed 형식 예외, 빈 사유, 빈 제목
Task 10: minor (deferred): 회차 간 같은 id 재채택 시 resolve/답글 기준 불일치, 한 thread에 지적 둘, 답글 문구에 내부 사유 노출, Codex 검토 열 중복 집계, 단건 제목 60자 해석(접두어 제외)
Task 10: fix round 1/5 dispatched (구현자 재개)
Task 10: fix round 1/5 (2 addressed, 0 open; commits 9dd2356..4923ae9)
Task 10: minor (deferred): _failed_ids가 회차 무관 id 기준, 빈 본문 다건 bullet 공백
Task 10: complete (commits 56a3838..4923ae9, review clean after 1 fix round)
Task 11: review (68634de) Critical 2 — 저장소 밖·gitignore 문서 미반영, 2회차부터 원래 스냅샷 기준(1회차 수정 안 보임). Important 2 — 빈 패치 합의-사실 채택, 상한 뒤 Critical 회차 재검토 없음. Minor: full_tests 비면 조용히 생략, report 이벤트, 보류 지적 집계 부풀림, model_id 빈값, next_steps 따옴표
- Ruling 14: 회차별 base_commit — apply 뒤 사용자 트리를 다시 스냅샷해 임시 브랜치를 전진, 다음 회차 워크트리·extract·재검토 diff는 그 커밋 기준 — 설계 8.4·10 — 틀리면 2회차 이후가 안 돎(리뷰어 재현).
- Ruling 15: 저장소 밖 문서 패치는 임시 디렉터리에 「외부/<이름>」로 복사해 git apply 후 원본에 덮어쓴다 — 설계 8.4 「저장소 밖 문서는 같은 상대 경로로 복사」의 역방향 — 틀리면 문서 모드 기본 경로 실패.
- Ruling 16: 빈 패치의 합의-사실은 채택하지 않고 판가름(패치 없음)으로 — 설계 4절 반목표 — Opus 호출이 조금 늘 수 있음.
- Ruling 17: 채택된 Critical이 있으면 재검토 상한 무시 — 설계 10절 5 — 반복 Critical 판단불가 규칙이 무한 반복을 막음.
Task 11: minor (deferred): 문서 모드 민감 경로 미검사, 보고서 판단 불가 칸 패치 경로가 -a/-b(문서는 -writer)
Task 11: fix round 1/5 dispatched (구현자 재개)
Task 11: fix round 1/5 (5 addressed, 0 open; commits 68634de..123c1e2)
Task 11: minor (deferred, 최종 검토에서 우선 처리): snapshot_tree가 전체 테스트 뒤에 찍혀 산출물(.coverage 등)이 재검토 diff·다음 base에 섞임 → 전체 테스트 전에 찍거나 채택 패치 경로만 diff
Task 11: minor (deferred): apply 중 예외 시 원장 미저장(재실행하면 전부 적용 실패), Ledger.docs list→dict 비호환, dry-run 2회차는 원래 스냅샷 기준, resume 뒤 report 안내 누락, 판가름 「아님」이 판단 불가 칸으로
Task 11: complete (commits 4923ae9..123c1e2, review clean after 1 fix round)
Task 12: review (05e725b) Important 2 — dry-run 재생에서 회귀가 항상 「통과」, --collect가 미완료 실행을 수집해 Opus 비율 왜곡. Minor: prune 제거, copytree try/finally, 종료 코드, id 검증, 워크트리 재사용 commit 확인
Task 12: minor (deferred): 문서 모드 실제 재생·패치 채택 경로의 fake 재생 미검증, repos.toml 키 심볼릭 링크 불일치
Task 12: fix round 1/5 dispatched (구현자 재개)
Task 12: fix round 1/5 (3 addressed, 0 open; commits 05e725b..6c9c6d8)
Task 12: complete (commits 123c1e2..6c9c6d8, review clean after 1 fix round)
== 모든 태스크 완료. 최종 브랜치 검토 시작 ==
최종 검토 (965c3b6..6c9c6d8): 수정 뒤 가능. Critical 5(포매터 전체 적용, 작업자 경계 없음, 외부 문서 위치 미변환, 참조 검사 전체 대상, 재현 명령 무확인 실행), Important 8, Minor 10 → final-review-findings.md
- Ruling 18: 재현 명령은 사용자 승인 목록(--allow-repro)이 없으면 건너뛰고 경고(멈추지 않음) — 보조 검증기라 없어도 합의 규칙은 성립 — 틀리면 PR 모드 검증기 하나가 빠짐.
- Ruling 19: 기준선에서 실패한 검증 명령은 판정에서 제외하고 「기준선 실패」로 기록 — 환경 탓 실패로 전부 Opus에 가는 것을 막음 — 틀리면 깨진 저장소에서 검증이 약해짐.
- Ruling 20: 워크트리는 .cascade-fix/에 그대로(.git/ 아래는 에이전트 쓰기 차단), 전체 테스트 전에 그 회차 워크트리를 제거 — I7 대응.
- Ruling 21: Workflow args는 promptPath만, 프롬프트는 파일로 — 컨트롤러 토큰 절감(I6).
- Ruling 22: dry-run은 result-rN 워크트리에 채택 패치를 적용·커밋해 다음 회차 기준으로 — I4.
최종 수정 파동 dispatched (1회)
최종 수정 파동 재검토 (6c9c6d8..dde8e70): C1~C5·I2~I8·Minor 1~10 해결. 잔여: Important 1(기준선 제외가 테스트 검증을 끔 → 깨진 패치 적용, 회귀), Minor 1(adjudicate가 repro-cmds.json 재독)
- Ruling 19' (19 대체): 기준선 제외는 build·typecheck·lint·format에만, tests·full_tests는 기준선과 무관하게 그대로 판정 — 리뷰어가 base 대비 회귀를 재현 — 틀리면 깨진 저장소에서 모든 지적이 Opus로 가는 비용 증가(안전 쪽 선택).
- Ruling 23: 승인된 재현 명령은 원장에 저장하고 adjudicate는 그 값만 쓴다 — 작업자가 쓸 수 있는 파일을 다시 읽지 않기 위해.
- Ruling 24: 「2차 수정 파동 없음」 규칙의 예외 — 잔여 2건은 컨트롤러 결정이 만든 안전 회귀라 같은 수정자가 좁게 고치고 테스트·짧은 재확인으로 닫는다.
잔여 수정 dispatched
