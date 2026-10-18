# codex-review 스킬 변경 기록

cascade-fix가 쓰는 `--recheck`·`--opinion` 모드를 `~/.claude/skills/codex-review/scripts/run-review.sh`에 더했다(프롬프트 파일을 stdin으로 `codex exec -s read-only --ephemeral -C ROOT -o out.final -`에 넘긴다). 그 저장소는 git 밖이라 변경을 diff로 여기 보관한다. 기존 `--uncommitted/--base/--commit/--doc` 모드는 바꾸지 않았다.

## 임시 CODEX_HOME에서 스킬 목록 끄기 (2026-10-18)

`run-review.sh`가 만드는 임시 CODEX_HOME의 설정에 다음 한 줄을 더했다.

```toml
skills.include_instructions = false
```

- 임시 home에서도 `~/.agents/skills`의 스킬 목록이 Codex 프롬프트에 들어가고 있었다. 이 설정으로 프롬프트가 약 37KB에서 약 11KB(실측 11,359바이트)로 줄어 Codex 이용량을 덜 쓴다.
- 이 변경은 다른 기기의 `run-review.sh`에만 있다. 그래서 이 폴더의 `run-review.sh.diff`에는 아직 들어 있지 않다. 그 기기에서 diff를 다시 만들어 이 폴더를 갱신해야 한다.
- 원래 CODEX_HOME을 함께 쓰는 방식은 쓰지 않는다. `--ignore-user-config`를 줘도 전역 AGENTS.md와 플러그인 스킬 목록이 프롬프트에 들어가는 것을 확인했다. 그래서 임시 CODEX_HOME을 유지한다.

## 남은 위험: refresh token

`run-review.sh`는 호출마다 `auth.json`을 임시 CODEX_HOME에 복사한다. 복사본 안에서 토큰이 갱신되면 한 번만 쓸 수 있는 refresh token이 소모되어 원본 `auth.json`이 무효가 될 수 있다.

- 갱신된 토큰을 원본에 되돌려 쓰는 근본 대책은 아직 넣지 않았다.
- 그동안 cascade-fix는 두 가지로 위험을 줄인다.
  - `RR`을 한 번에 하나씩 돌린다(SKILL.md 「규칙」).
  - 결과가 없거나 비면 실행 로그와 인증 실패 흔적을 보이고 멈춘다(`opinion`·`recheck`).
