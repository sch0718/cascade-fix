# cascade-fix

리뷰에서 나온 지적을 Claude Code가 **지적 하나씩** 고치게 하는 스킬입니다.

- 싼 작업자 둘(Sonnet 5.5·Haiku 5.5)이 서로 다른 워크트리에서 같은 지적을 따로 판정하고 고칩니다.
- 스크립트가 테스트·빌드·린트를 다시 돌리고, 두 결과가 맞으면 채택합니다.
- 갈린 것만 Opus 5.5가 판가름합니다.
- 그래도 못 정한 것은 사람이 볼 목록으로 넘깁니다.

커밋·push·PR 코멘트 resolve·답글 게시는 하지 않습니다. 커밋 메시지 초안과 답글 초안만 「준비됨」으로 만들어 두고, 누르는 것은 사람입니다.

```
리뷰 지적 ─┬─ 작업자 A (Sonnet) ─┐
           └─ 작업자 B (Haiku)  ─┴─ 기계 검증 + 합의 ─┬─ 채택 → 작업 트리에 적용 → 재검토
                                                    └─ 갈림 → Opus 판가름 ─┬─ 채택
                                                                          └─ 판단 불가 → 검토자 의견 + 사람 목록
```

## 무엇을 고칠 수 있나

| 입력 | `--source` | 설명 |
| --- | --- | --- |
| Claude 리뷰 | `claude-code` · `claude-doc` | 커밋하지 않은 변경, 브랜치 diff, 특정 커밋, 설계·계획 문서를 Claude가 먼저 검토하고 그 지적을 고칩니다. Codex 없이 돕니다. 설계 단계에서는 스펙·계획서 초안을 구현 전에 검토하는 데 씁니다([설계 단계에서 쓰기](#설계-단계에서-쓰기)). |
| PR 리뷰 코멘트 | `pr-comment` | 해결 안 된 리뷰 스레드를 지적으로 읽고 고칩니다. 답글 초안과 resolve 대상도 만듭니다. 재검토 회차는 열지 않습니다. `gh` CLI가 필요합니다. |
| Codex 리뷰 결과 | `codex-code` · `codex-doc` | `codex-review` 스킬의 `run-review.sh` 결과 파일(`*.final`)을 받아 고칩니다. |

`*-code`와 `pr-comment`는 코드 모드라 저장소 검증 명령(`tests`)이 등록되어 있어야 합니다. `*-doc`은 문서 모드라 검증 명령 없이 돕니다. 코드 모드는 지적마다 작업자 둘이, 문서 모드는 작성자 하나와 판정자 둘이 붙습니다.

적합하지 않은 일도 있습니다.
- 문서를 처음부터 새로 쓰는 일(초안 작성). 초안이 나온 뒤의 검토-수정은 맡길 수 있습니다.
- 여러 파일에 걸친 리팩터링
- 테스트가 없는 저장소의 코드 수정
- 스펙이 모호해 「맞다」의 기준이 없는 지적

보안·인증·삭제 경로(`**/auth*`, `**/security*`, `**/*delete*`, `**/migrations/**`)를 고친 지적은 채택하지 않고 사람 목록으로 보냅니다.

## 요구 사항

- Claude Code. 여러 에이전트를 띄우는 **Workflow 도구**를 쓸 수 있어야 합니다.
- Python 3.11 이상. 외부 패키지는 필요 없습니다.
- git
- (PR 모드) 로그인된 `gh` CLI
- (선택) Codex를 검토자로 쓰려면 `codex-review` 스킬과 `codex` CLI. 이 스킬은 `run-review.sh`의 `--opinion`·`--recheck` 모드를 씁니다. 원래 `codex-review`에는 없는 모드라 [docs/codex-review-changes/](docs/codex-review-changes/README.md)의 diff를 먼저 적용해야 합니다. 같은 문서에 임시 CODEX_HOME에서 스킬 목록을 끄는 설정도 적어 두었습니다. 없으면 검토자는 자동으로 Claude가 됩니다.

## 설치

설치 방법은 둘 중 하나만 고릅니다. 둘 다 설치하면 같은 이름의 스킬이 겹칩니다.

이 저장소는 비공개입니다. 어느 방법이든 저장소에 접근할 수 있는 GitHub 계정으로 git 인증(예: `gh auth login`)이 되어 있어야 합니다.

### 1. 플러그인 마켓플레이스로 설치 (권장)

Claude Code 세션에서:

```
/plugin marketplace add sch0718/cascade-fix
/plugin install cascade-fix@sch0718
```

터미널에서:

```bash
claude plugin marketplace add sch0718/cascade-fix
```

```bash
claude plugin install cascade-fix@sch0718
```

업데이트와 삭제:

```bash
claude plugin marketplace update sch0718
```

```bash
claude plugin update cascade-fix@sch0718
```

```bash
claude plugin uninstall cascade-fix@sch0718
```

- 새 버전은 다음 세션부터, 또는 `/reload-plugins` 뒤에 적용됩니다.
- 서드파티 마켓플레이스는 기본적으로 자동 업데이트가 꺼져 있습니다.

### 2. 개인 스킬로 설치

```bash
git clone https://github.com/sch0718/cascade-fix.git ~/src/cascade-fix
```

```bash
ln -s ~/src/cascade-fix ~/.claude/skills/cascade-fix
```

업데이트는 `git -C ~/src/cascade-fix pull`로 합니다.

## 처음 한 번: 저장소 검증 명령 등록

코드를 고치는 모드는 저장소마다 테스트 명령이 등록되어 있어야 합니다. 테스트 없이 합의만으로는 틀린 수정을 걸러낼 수 없기 때문입니다.

**등록은 직접 하지 않아도 됩니다.** 스킬이 `package.json`·`pyproject.toml`·`Makefile`·`tests/`를 보고 후보를 찾아 확인을 받은 뒤 대신 적어 줍니다.

직접 적으려면 `~/.claude/cascade-fix/repos.toml`에 씁니다. 이 파일은 플러그인을 업데이트해도 남습니다.

```toml
[repos."/Users/me/work/my-app"]
tests = ["pytest {files}"]          # 바뀐 파일과 관련된 테스트만 돌리는 명령
full_tests = ["pytest"]             # 적용 뒤 한 번 돌리는 전체 테스트
lint = ["ruff check ."]
format = ["ruff format {files}"]    # 작업자가 바꾼 파일에만 돈다
# build / typecheck / setup / sensitive_paths / doc_snippet_check 도 쓸 수 있다
```

전체 형식은 [repos.toml](repos.toml)의 머리 주석에 있습니다. 다른 파일을 쓰려면 환경 변수 `CASCADE_REPOS_TOML`로 지정합니다.

## 사용법

### 말로 맡기기 (기본)

Claude Code에 평소처럼 말하면 상황에 맞춰 알아서 진행합니다.

```
이번 변경 리뷰하고 고쳐줘
PR 코멘트 반영해줘
docs/plan.md 리뷰 지적 고쳐줘
스펙 초안(docs/superpowers/specs/…-design.md) 검토하고 고쳐줘
Codex 검토 결과(review.final) 반영해줘
```

진행 순서는 이렇습니다.

1. **상황 판정**: `detect`가 저장소를 읽기만 하고 무엇을 처리할지 추천합니다. 판단 순서는 다음과 같습니다.
   - 결과 파일이 있는가
   - 검토할 문서를 지정했는가(`--doc`)
   - PR에 해결 안 된 리뷰 스레드가 있는가
   - 커밋하지 않은 변경이 있는가
   - 기준 브랜치보다 앞선 커밋이 있는가

   바뀐 파일이 모두 문서면 문서 모드로 갑니다. 코드와 문서가 섞였으면 코드 모드로 가고, 문서만 따로 볼 명령을 함께 알려 줍니다. 테스트가 없거나 처리할 것이 없으면 여기서 멈춥니다.
2. **계획 확인 한 번**: 입력·모드·검토자와 예상 에이전트 수를 보여 줍니다. 「진행 / Critical·Warning만 / dry-run」 중에서 고릅니다. 검증 명령 등록이 필요하면 이때 함께 확인합니다.
3. **끝까지 진행**: 리뷰, 작업자 2명, 검증, 판가름, 적용, 재검토를 차례로 돕니다. 단계마다 다시 묻지 않습니다.
   - 적용 뒤 재검토에서 새 지적이 나오면 다음 회차를 엽니다. 재검토는 실행당 최대 2회입니다(`--max-rechecks`).
   - 앞 패치와 같은 파일을 고쳐 적용하지 못한 지적은 다음 회차에서 바뀐 코드로 다시 고칩니다.
4. **보고**: 회차별 표와 사람이 볼 목록 네 칸을 보여 줍니다.
   - 반박
   - 설계 결정: 판정자 근거와, 문서 모드면 작성자가 낸 선택지·장단점(작성자가 안 하나를 골라 고쳤으면 그 수정안과 패치 경로)
   - 판단 불가
   - 보류 후보

   커밋 메시지 초안 등 「준비됨」 파일도 함께 알려 줍니다.

중간에 멈추는 경우는 다음뿐입니다.
- 검증 설정이 없을 때
- 리뷰 결과를 해석하지 못할 때
- 작업 중 사용자 저장소가 바뀌었을 때
- PR 재현 명령의 실행 승인이 필요할 때
- 모델 ID가 기대와 다를 때

### 설계 단계에서 쓰기

초안을 쓰는 일은 맡기지 않고, 초안이 나온 뒤 구현에 들어가기 전에 검토-수정을 맡깁니다.

- superpowers를 쓴다면 brainstorming이 스펙을 쓰고 자체 검토를 마친 뒤, 또 writing-plans가 계획서를 쓰고 자체 검토를 마친 뒤에 붙입니다. 이 스킬의 보고를 그다음 사용자 검토 때 함께 봅니다.
- 스펙은 쓰자마자 커밋되는 일이 많아 변경으로 잡히지 않습니다. 「docs/…/spec.md 검토하고 고쳐줘」처럼 문서를 말하면 `detect --doc`으로 판정합니다. 검증 명령 등록은 필요 없습니다.
- 구현 직전의 계획서는 지적이 구체적이라 자동 채택이 많습니다. 초기 스펙은 「정하지 않은 결정」 지적이 많아 「설계 결정」 칸으로 모입니다. 이것이 정상 결과입니다.
- 「설계 결정」 칸에는 선택지와 장단점이 붙습니다. 결정은 사람이 합니다. 고른 대로 문서를 고친 뒤 다시 검토합니다.
- 작성자가 선택지 대신 안 하나를 골라 고쳤으면 「작성자 수정안(채택 안 함)」과 패치 경로가 붙습니다. 그 안을 쓰기로 하면 Claude에게 맡기세요. 패치를 적용하고 다시 검토합니다.

### 직접 부르기

- 플러그인으로 설치했으면 `/cascade-fix:cascade-fix`, 개인 스킬이면 `/cascade-fix`로 부릅니다.
- 옵션을 함께 말하면("Critical만, dry-run으로, 묻지 말고 진행") 계획 확인을 건너뜁니다.

### 검토자: Codex 또는 Claude 단독

판가름 뒤 「의견」과 적용 뒤 「재검토」를 누가 맡을지 고릅니다.

| 설정 | 동작 |
| --- | --- |
| `auto` (기본) | Claude 리뷰로 시작했거나 `codex-review` 스킬이 없으면 Claude, 아니면 Codex |
| `claude` | 의견·재검토를 Claude 에이전트가 맡습니다. 외부(OpenAI) 전송이 없습니다. |
| `codex` | `run-review.sh --opinion/--recheck`로 Codex에 보냅니다. 저장소마다 전송 동의를 받습니다. 호출은 한 번에 하나씩 돌립니다(동시에 토큰을 갱신하면 로그인이 풀릴 수 있습니다). 결과가 없으면 실행 로그와 인증 실패 여부를 보여 주고 멈춥니다. |

- 기본값은 환경 변수 `CASCADE_REVIEWER`로 바꿉니다.
- Claude 검토자의 기본 모델은 `claude-fable-5-1`(effort `high`)입니다. 판가름을 맡는 Opus와 다른 모델을 써서 같은 판단을 되풀이하지 않게 하려는 것입니다.
- 모델은 `--review-model`, effort는 `--review-effort`로 바꿉니다.

### 비용

에이전트 수는 지적 수에 비례합니다. 지적 1건마다 아래만큼 붙습니다.

| 모드 | 작업자 | 추가로 붙을 수 있는 것 |
| --- | --- | --- |
| 코드 | 2 | 판가름 최대 1 |
| 문서 | 3 | 점검자·판가름 최대 1씩 |

Claude 검토자면 판단 불가 1건마다 의견 1이 더 붙고, 재검토는 회차마다 1입니다.

- `detect`와 `init`이 예상 개수를 먼저 보여 줍니다.
- 지적이 많으면 `--severity Critical,Warning`으로 먼저 돌립니다. 빠진 등급은 보류 후보로 남고, 나중에 「지금 반영」으로 이어서 처리할 수 있습니다.

## 결과물

모든 상태는 대상 저장소의 `.cascade-fix/<run_id>/`에 남습니다. git 추적에서는 자동으로 빠집니다.

- `ledger.json`: 원장. 중단돼도 `status` 명령으로 이어 갈 수 있습니다.
- `report.md`: 회차별 표와 사람이 볼 목록
- `prepared/commit-message.txt`: 커밋 메시지 초안
- `prepared/replies.json`: PR 답글 초안 (PR 모드)
- `prepared/resolve.json`: resolve 대상 (PR 모드)
- `round-N/patch-*.diff`: 작업자·판가름자 패치

채택한 패치는 작업 트리에 적용되고 커밋되지는 않습니다. `--dry-run`이면 작업 트리를 건드리지 않고 임시 워크트리에만 적용합니다. 끝나면 스킬이 `cleanup`으로 임시 브랜치와 워크트리를 지웁니다.

## CLI 레퍼런스

스킬이 내부에서 부르는 명령입니다. 직접 쓸 일은 거의 없습니다. `<스킬 경로>`는 플러그인이면 `~/.claude/plugins/cache/sch0718/cascade-fix/<버전>`, 개인 스킬이면 `~/.claude/skills/cascade-fix`입니다.

```bash
PYTHONPATH=<스킬 경로> python3 -m cascade.cli <명령> [옵션]
```

| 명령 | 하는 일 |
| --- | --- |
| `detect [--input F] [--doc F…] [--pr N] [--no-gh] [--reviewer R] [--json]` | 상황을 판정하고 추천·규모·멈춤 사유를 보여 줍니다. 읽기만 합니다. `--doc`은 커밋한 문서도 검토 대상으로 지정합니다. `--no-gh`면 PR을 조회하지 않습니다. |
| `review --kind code\|doc (--base B \| --uncommitted \| --commit SHA \| --doc F…) [--focus 관점] [--review-model M] [--review-effort E]` | Claude 첫 검토용 Workflow 입력을 만듭니다. |
| `init --source S --input X [--doc F…] [--severity …] [--dry-run] [--max-rechecks N] [--reviewer R] [--review-model M] [--review-effort E]` | 실행을 시작하고 지적을 읽습니다. PR이면 `--input`에 PR 번호를 주고 `--pr-repo owner/name`·`--me <내 로그인>`을 붙일 수 있습니다. `--input` 대신 `--findings-json F`로 정규화한 지적 목록(`id`·`severity`·`location`·`body`)을 바로 줄 수도 있습니다. |
| `jobs` / `judge` / `adjudicate` / `opinion` / `apply` / `recheck` | 회차 단계(`--run-dir`·`--round`) |
| `resume --choices F` | 보류 후보마다 「지금 반영 / 구현 때 테스트로 / 보류」를 받습니다. 「지금 반영」이 있으면 회차를 엽니다. |
| `report` / `status` / `cleanup` | 보고서, 다음 할 일, 정리 |

- `detect`·`review`·`init`은 `--root`로 대상 저장소 루트를 받습니다. 없으면 현재 디렉터리의 저장소입니다. 나머지 명령은 `init`이 출력한 `--run-dir`로 실행을 찾습니다.
- 회차 단계는 이미 한 단계를 다시 하지 않습니다. 다시 하려면 `--force`를 붙입니다.
- 각 명령은 끝에 「다음:」으로 다음에 할 명령을 출력합니다. 단계 상세는 [SKILL.md](SKILL.md)를 보세요.

종료 코드:

| 코드 | 뜻 |
| --- | --- |
| 4 | 저장소 검증 설정이 없습니다. 후보 명령을 함께 출력합니다. |
| 5 | 리뷰 결과·설정 파일을 해석하지 못했습니다. |
| 6 | 사람이 봐야 합니다. `detect`가 멈출 사유를 냈거나, 작업 중 사용자 저장소(작업 트리·HEAD·stash·브랜치)가 바뀌었습니다. |

환경 변수:

| 변수 | 뜻 |
| --- | --- |
| `CASCADE_REPOS_TOML` | 검증 설정 파일 경로 |
| `CASCADE_REVIEWER` | 기본 검토자(`auto`/`claude`/`codex`) |
| `CASCADE_RUN_REVIEW` | `codex-review`의 `run-review.sh` 경로 |
| `CASCADE_GH` | `gh` 대신 쓸 명령 |

## 개발

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

- 테스트는 320개입니다. `codex-review` 스킬이 없으면 그 스크립트를 쓰는 테스트 10개는 건너뜁니다.
- 디렉터리: `cascade/`(CLI와 단계 로직), `prompts/`(작업자·판정자·판가름·검토자 프롬프트), `scripts/run-jobs.js`(Workflow 스크립트), `eval/`(측정 도구), `docs/`(설계 검토 기록과 `codex-review` 변경 diff)
- 측정: `eval/replay.py`가 과거 지적 표본을 그 커밋에서 다시 돌리고, `eval/score.py`가 판정 일치율·채택 경로·Opus 호출 절감을 채점합니다. 표본 형식과 순서는 [eval/sample-format.md](eval/sample-format.md)에 있습니다.
- 출력 스키마를 바꾸면 `cascade/jobs.py`의 `SCHEMAS`와 `scripts/run-jobs.js`의 `SCHEMAS` 블록을 같이 고칩니다(테스트가 두 쪽이 같은지 확인합니다).
- 배포할 때는 `.claude-plugin/plugin.json`의 `version`을 올립니다. 버전이 같으면 설치한 사람에게 업데이트로 보이지 않습니다.
- 매니페스트 검사: `claude plugin validate .`
