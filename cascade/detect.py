"""저장소 상태를 읽어 cascade-fix를 어떻게 돌릴지 추천한다. 읽기만 하고 아무것도 바꾸지 않는다."""

import json
import re
import shlex
import subprocess
from pathlib import Path

from cascade.config import ConfigError, RepoConfig, detect_candidates, is_sensitive_path, load_repo_config
from cascade.findings import ParseError, fetch_pr_findings, parse_codex_code, parse_codex_doc
from cascade.review import REVIEWER_AUTO, REVIEWER_CLAUDE, label, resolve
from cascade.snapshot import RUN_DIR_NAME

DOC_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
LARGE_CHANGE_FILES = 30
SEVERITY_SPLIT_JOBS = 10  # 1단계 작업이 이보다 많으면 등급별로 나눠 돌리는 안을 낸다
_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _git(root: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=root, capture_output=True, text=True)
    return p.returncode, p.stdout


def uncommitted_files(root: Path) -> list[str]:
    code, out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if code != 0:
        return []
    files: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # 이름 바꾸기는 원래 이름이 다음 칸에 온다
        if not path.startswith(f"{RUN_DIR_NAME}/"):
            files.append(path)
    return files


def default_base(root: Path) -> str | None:
    code, out = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and out.strip():
        return out.strip()
    for name in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")[0] == 0:
            return name
    return None


def ahead_of(root: Path, base: str) -> tuple[int, list[str]]:
    code, out = _git(root, "rev-list", "--count", f"{base}..HEAD")
    commits = int(out.strip()) if code == 0 and out.strip().isdigit() else 0
    if not commits:
        return 0, []
    code, out = _git(root, "diff", "--name-only", f"{base}...HEAD")
    return commits, [line for line in out.splitlines() if line.strip()] if code == 0 else []


def current_branch(root: Path) -> str:
    code, out = _git(root, "branch", "--show-current")
    return out.strip() if code == 0 else ""


def pr_info(root: Path, gh: list[str], number: str | None = None) -> tuple[dict | None, str | None]:
    """현재 브랜치(또는 번호)의 PR과 해결 안 된 리뷰 스레드 수. 실패하면 (None, 사유)."""
    try:
        p = subprocess.run([*gh, "pr", "view", *([number] if number else []), "--json", "number,url,state"],
                           cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"gh 실행 실패: {e}"
    if p.returncode != 0:
        return None, (p.stderr.strip().splitlines() or ["PR 없음"])[0]
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return None, "gh pr view 출력을 읽지 못했다"
    m = _PR_URL.search(data.get("url", ""))
    if not m:
        return None, f"PR 주소를 해석하지 못했다: {data.get('url')}"
    owner, name, num = m.group(1), m.group(2), int(m.group(3))
    me = subprocess.run([*gh, "api", "user", "--jq", ".login"], capture_output=True, text=True).stdout.strip()
    try:
        findings = fetch_pr_findings(owner, name, num, me, gh)
    except (RuntimeError, ValueError, KeyError) as e:
        return {"number": num, "url": data.get("url"), "state": data.get("state"), "repo": f"{owner}/{name}",
                "unresolved": None}, f"리뷰 스레드를 읽지 못했다: {e}"
    return {"number": num, "url": data.get("url"), "state": data.get("state"), "repo": f"{owner}/{name}",
            "unresolved": len(findings), "me": me}, None


def classify_input(path: Path) -> tuple[str | None, int]:
    """결과 파일이 어떤 --source 인지와 지적 수를 추정한다."""
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and "results" in data:
        return "claude-code", -1
    code = parse_codex_code(raw, 1)
    if code:
        return "codex-code", len(code)
    try:
        return "codex-doc", len(parse_codex_doc(raw, 1))
    except ParseError:
        return None, 0


def estimate(findings: int, mode: str, reviewer: str) -> dict:
    """한 회차의 에이전트 수. 판가름·의견은 갈린 지적에만 붙으므로 최대치다."""
    per = 3 if mode == "doc" else 2
    out = {
        "stage1": findings * per,
        "checker_max": findings if mode == "doc" else 0,
        "adjudicator_max": findings,
        "opinion_max": findings if reviewer == REVIEWER_CLAUDE else 0,
        "recheck": 1 if reviewer == REVIEWER_CLAUDE else 0,
    }
    out["min"] = out["stage1"]
    out["max"] = sum(v for k, v in out.items() if k not in ("min",))
    return out


def estimate_line(findings: int, mode: str, reviewer: str) -> str:
    e = estimate(findings, mode, reviewer)
    parts = [f"작업자 {e['stage1']}"]
    if e["checker_max"]:
        parts.append(f"점검자 최대 {e['checker_max']}")
    parts.append(f"판가름 최대 {e['adjudicator_max']}")
    if e["opinion_max"]:
        parts.append(f"의견 최대 {e['opinion_max']}")
    if e["recheck"]:
        parts.append(f"재검토 {e['recheck']}")
    return f"회차당 에이전트 {e['min']}~{e['max']}개({', '.join(parts)})"


def _config(root: Path, repos_toml: Path) -> tuple[RepoConfig | None, str | None]:
    try:
        return load_repo_config(root, repos_toml), None
    except (ConfigError, ValueError) as e:
        return None, f"검증 설정을 읽지 못했다({repos_toml}): {e}"


def detect(root: Path, *, repos_toml: Path, run_review: Path, cli: str, gh: list[str] | None,
           input_path: Path | None = None, pr: str | None = None, reviewer_choice: str = REVIEWER_AUTO) -> dict:
    root = Path(root)
    branch = current_branch(root)
    base = default_base(root)
    uncommitted = uncommitted_files(root)
    commits, ahead = ahead_of(root, base) if base else (0, [])
    notes: list[str] = []
    blockers: list[str] = []
    pr_data = None
    if gh is not None and (pr or not input_path):
        pr_data, why = pr_info(root, gh, pr)
        if why and (pr or pr_data):
            notes.append(why)

    q = shlex.quote
    rec: dict = {"source": None, "mode": None, "steps": [], "reasons": []}
    found = None
    if input_path is not None:
        source, found = classify_input(input_path)
        if source is None:
            blockers.append(f"결과 파일 형식을 알 수 없다: {input_path}")
        else:
            rec.update(source=source, mode="doc" if source.endswith("doc") else "code")
            rec["reasons"].append(f"결과 파일이 {source} 형식이다" + (f"(지적 {found}건)" if found >= 0 else ""))
    elif pr_data and pr_data.get("unresolved"):
        found = pr_data["unresolved"]
        rec.update(source="pr-comment", mode="code")
        rec["reasons"].append(f"PR #{pr_data['number']}에 해결 안 된 리뷰 스레드 {found}건이 있다")
    elif uncommitted or ahead:
        changed = uncommitted or ahead
        doc_only = all(p.endswith(DOC_SUFFIXES) for p in changed)
        kind = "doc" if doc_only else "code"
        rec.update(source=f"claude-{kind}", mode=kind)
        if uncommitted:
            rng, why = "--uncommitted", f"커밋하지 않은 변경 {len(uncommitted)}파일"
        else:
            rng, why = f"--base {q(base)}", f"{base}보다 커밋 {commits}개 앞섬({len(ahead)}파일)"
        rec["reasons"].append(why + (" — 모두 문서라 문서 모드" if doc_only else ""))
        if doc_only:
            rec["review_args"] = " ".join(f"--doc {q(p)}" for p in changed)
        else:
            rec["review_args"] = rng
        if len(changed) > LARGE_CHANGE_FILES:
            notes.append(f"바뀐 파일이 {len(changed)}개다. 지적이 많으면 --severity 로 나눠 돌린다")
    else:
        blockers.append("처리할 것이 없다: 결과 파일·PR 리뷰 스레드·커밋하지 않은 변경·앞선 커밋이 모두 없다")

    reviewer = resolve(reviewer_choice, rec["source"] or "", run_review)
    cfg, cfg_error = _config(root, repos_toml)
    if cfg_error:
        blockers.append(cfg_error)
    config = {"path": str(repos_toml), "registered": cfg is not None, "tests": list(cfg.tests) if cfg else []}
    approvals: list[str] = []
    if rec["mode"] == "code" and not (cfg and cfg.tests):
        candidates = detect_candidates(root)
        config["candidates"] = candidates
        if candidates.get("tests"):
            approvals.append(f"검증 명령 등록: {repos_toml} 에 [repos.\"{root}\"] 항목을 후보로 적는다(사용자 확인)")
        else:
            blockers.append("코드 모드인데 테스트 명령을 찾지 못했다. 테스트 없는 저장소의 코드 수정은 이 스킬로 하지 않는다")
    if reviewer != REVIEWER_CLAUDE:
        approvals.append("Codex 전송 동의: 의견·재검토에 코드·문서를 OpenAI로 보낸다")
    if rec["source"] == "pr-comment":
        approvals.append("PR 코멘트의 재현 명령이 있으면 jobs 뒤에 목록을 보이고 승인받는다")

    patterns = (cfg or RepoConfig()).sensitive_paths
    changed_all = list(dict.fromkeys(uncommitted + ahead))
    sensitive = [p for p in changed_all if is_sensitive_path(p, patterns)]
    if sensitive:
        notes.append("보안·인증·삭제 경로가 바뀌었다. 이 경로를 고친 지적은 채택하지 않고 판단 불가로 보낸다: " + ", ".join(sensitive[:5]))
    if uncommitted and rec["source"] in ("pr-comment", "codex-code", "codex-doc"):
        notes.append("커밋하지 않은 변경이 있다. 채택 패치가 작업 트리에 섞이지 않게 하려면 --dry-run 을 쓴다")

    steps = rec["steps"]
    flags = "" if reviewer_choice == REVIEWER_AUTO else f" --reviewer {reviewer_choice}"
    if not blockers and rec["source"]:
        if rec["source"].startswith("claude-") and input_path is None:
            steps.append(f"{cli} review --kind {rec['mode']} {rec['review_args']}")
            steps.append("Workflow(첫 검토) → review.json 저장 → review 출력의 init 명령")
        elif rec["source"] == "pr-comment":
            steps.append(f"{cli} init --source pr-comment --input {pr_data['number']} --pr-repo {q(pr_data['repo'])}"
                         + (f" --me {q(pr_data['me'])}" if pr_data.get("me") else "") + flags)
        else:
            steps.append(f"{cli} init --source {rec['source']} --input {q(str(input_path))}{flags}")
    if found is not None and found > 0 and rec["mode"]:
        rec["estimate"] = estimate(found, rec["mode"], reviewer)
        if rec["estimate"]["stage1"] > SEVERITY_SPLIT_JOBS:
            notes.append(f"1단계 작업이 {rec['estimate']['stage1']}개다. --severity Critical,Warning 으로 먼저 돌리는 안을 함께 제시한다")

    return {
        "root": str(root),
        "branch": branch,
        "base": base,
        "uncommitted": uncommitted,
        "ahead": {"base": base, "commits": commits, "files": ahead},
        "pr": pr_data,
        "reviewer": reviewer,
        "reviewer_label": label(reviewer),
        "config": config,
        "recommendation": rec,
        "approvals": approvals,
        "notes": notes,
        "blockers": blockers,
        "ok": not blockers,
    }


def render(result: dict) -> str:
    rec = result["recommendation"]
    lines = [f"저장소: {result['root']} (브랜치 {result['branch'] or '?'}, 기준 {result['base'] or '없음'})"]
    lines.append(f"커밋하지 않은 변경: {len(result['uncommitted'])}파일 · 앞선 커밋: {result['ahead']['commits']}개")
    pr = result["pr"]
    if pr:
        lines.append(f"PR: #{pr['number']} {pr.get('state', '')} · 해결 안 된 리뷰 스레드 {pr['unresolved'] if pr['unresolved'] is not None else '?'}건")
    if rec["source"]:
        lines.append(f"추천: --source {rec['source']} · 모드 {rec['mode']} · 검토자 {result['reviewer_label']}")
        lines.extend(f"  근거: {r}" for r in rec["reasons"])
    if rec.get("estimate"):
        e = rec["estimate"]
        lines.append(f"규모: 에이전트 {e['min']}~{e['max']}개(1회차 기준)")
    elif rec["source"]:
        e = estimate(1, rec["mode"], result["reviewer"])
        lines.append(f"규모: 지적 1건당 에이전트 {e['stage1']}~{e['max'] - e['recheck']}개"
                     + (f" + 재검토 회차당 {e['recheck']}개" if e["recheck"] else "") + " (지적 수는 첫 검토 뒤에 정해진다)")
    for a in result["approvals"]:
        lines.append(f"확인 필요: {a}")
    for n in result["notes"]:
        lines.append(f"참고: {n}")
    for b in result["blockers"]:
        lines.append(f"멈춤: {b}")
    if rec["steps"]:
        lines.append("다음:")
        lines.extend(f"  {step}" for step in rec["steps"])
    lines.append(f"ok={'예' if result['ok'] else '아니오'}")
    return "\n".join(lines)
