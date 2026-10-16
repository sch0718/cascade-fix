# codex-review 스킬 변경 기록

cascade-fix가 쓰는 `--recheck`·`--opinion` 모드를 `~/.claude/skills/codex-review/scripts/run-review.sh`에 더했다(프롬프트 파일을 stdin으로 `codex exec -s read-only --ephemeral -C ROOT -o out.final -`에 넘긴다). 그 저장소는 git 밖이라 변경을 diff로 여기 보관한다. 기존 `--uncommitted/--base/--commit/--doc` 모드는 바꾸지 않았다.
