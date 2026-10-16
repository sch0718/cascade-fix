import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ("id", "repo", "commit", "source", "finding", "truth", "baseline_opus_calls")
SOURCES = ("codex-doc", "codex-code", "pr-comment")
ROUND = "1"


def load_sample(path) -> list[dict]:
    items = []
    for no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        missing = [k for k in REQUIRED if k not in item]
        if missing:
            raise ValueError(f"{path}:{no} 에 {', '.join(missing)} 가 없다")
        if item["source"] not in SOURCES:
            raise ValueError(f"{path}:{no} 알 수 없는 source: {item['source']}")
        items.append(item)
    return items


def git(cwd, *args, check=True) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {(p.stderr or p.stdout).strip()}")
    return p.stdout.strip()


def toml_value(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def write_worktree_toml(src: Path, repo: Path, wt: Path, dest: Path) -> None:
    data = tomllib.loads(src.read_text(encoding="utf-8")) if src.is_file() else {}
    entry = data.get("repos", {}).get(str(repo.resolve()))
    lines = ["[repos]"]
    if entry is not None:
        key = json.dumps(str(wt))
        tables = {k: v for k, v in entry.items() if isinstance(v, dict)}
        lines += [f"[repos.{key}]", *(f"{k} = {toml_value(v)}" for k, v in entry.items() if k not in tables)]
        for name, table in tables.items():
            lines += [f"[repos.{key}.{name}]", *(f"{k} = {toml_value(v)}" for k, v in table.items())]
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cli(wt: Path, env: dict, *args) -> str:
    p = subprocess.run([sys.executable, "-m", "cascade.cli", *args], cwd=wt, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"cli {args[0]} 실패({p.returncode}): {(p.stdout + p.stderr).strip()[-1500:]}")
    return p.stdout


def doc_arg(doc: str, repo: Path, wt: Path) -> str:
    path = Path(doc)
    if not path.is_absolute():
        return str(wt / path)
    try:
        return str(wt / path.relative_to(repo))
    except ValueError:
        return doc


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def finish_item(base: Path, repo: Path, wt: Path, run_dir: Path | None, env: dict) -> None:
    errors = []
    try:
        if run_dir is not None and (run_dir / "ledger.json").is_file():
            try:
                cli(wt, env, "cleanup", "--run-dir", str(run_dir))
            except RuntimeError as e:
                errors.append(str(e))
            shutil.rmtree(base / "run", ignore_errors=True)
            shutil.copytree(run_dir, base / "run")
            shutil.copyfile(run_dir / "ledger.json", base / "ledger.json")
    finally:
        if wt.exists():
            git(repo, "worktree", "remove", "--force", str(wt), check=False)
    if errors:
        raise RuntimeError("; ".join(errors))


def base_env(toml: Path) -> dict:
    env = dict(os.environ)
    env.update(CASCADE_REPOS_TOML=str(toml), PYTHONPATH=str(SKILL_ROOT), PYTHONDONTWRITEBYTECODE="1")
    return env


def run_item(item: dict, out: Path, repos_toml: Path, fake: Path | None) -> None:
    repo = Path(item["repo"]).resolve()
    base = (out / item["id"]).resolve()
    wt = base / "wt"
    base.mkdir(parents=True, exist_ok=True)
    meta = {"truth": item["truth"], "baseline_opus_calls": item["baseline_opus_calls"]}
    write_json(base / "meta.json", meta)
    run_dir = None
    env = base_env(base / "repos.toml")
    try:
        if not wt.exists():
            git(repo, "worktree", "add", "--detach", str(wt), item["commit"])
        else:
            head, wanted = git(wt, "rev-parse", "HEAD"), git(repo, "rev-parse", "--verify", f"{item['commit']}^{{commit}}")
            if head != wanted:
                raise RuntimeError(f"기존 워크트리의 HEAD {head[:12]}가 표본의 커밋 {wanted[:12]}와 다르다")
        wt = wt.resolve()
        write_worktree_toml(repos_toml, repo, wt, base / "repos.toml")
        findings = base / "findings-input.json"
        write_json(findings, [item["finding"]])
        args = ["init", "--source", item["source"], "--findings-json", str(findings), "--dry-run"]
        if item.get("doc"):
            args += ["--doc", doc_arg(item["doc"], repo, wt)]
        out_text = cli(wt, env, *args)
        run_dir = Path(next(x for x in out_text.splitlines() if x.startswith("run_dir=")).split("=", 1)[1])
        rd = str(run_dir)
        cli(wt, env, "jobs", "--run-dir", rd, "--round", ROUND)
        if fake is None:
            write_json(base / "meta.json", {**meta, "pending_run_dir": rd})
            print(f"{item['id']}: 작업 파일 {run_dir / 'round-1' / 'stage1-args.json'}")
            print("  스킬의 Workflow로 stage1을 돌리고 judge → adjudicate → apply → report 를 이어간 뒤 --collect 로 모은다")
            return
        item_fake = fake / item["id"]
        stage1 = run_dir / "round-1" / "stage1.json"
        shutil.copyfile(item_fake / "stage1.json", stage1)
        judge = ["judge", "--run-dir", rd, "--round", ROUND, "--results", str(stage1)]
        if (item_fake / "checker.json").is_file():
            checker = run_dir / "round-1" / "checker.json"
            shutil.copyfile(item_fake / "checker.json", checker)
            judge += ["--checker-results", str(checker)]
        cli(wt, env, *judge)
        if (item_fake / "adj.json").is_file():
            adj = run_dir / "round-1" / "adj.json"
            shutil.copyfile(item_fake / "adj.json", adj)
            cli(wt, env, "adjudicate", "--run-dir", rd, "--round", ROUND, "--results", str(adj))
        cli(wt, env, "apply", "--run-dir", rd, "--round", ROUND)
        cli(wt, env, "report", "--run-dir", rd)
    except Exception as e:
        write_json(base / "meta.json", {**meta, "error": str(e)})
        finish_item(base, repo, wt, run_dir, env)
        raise
    finish_item(base, repo, wt, run_dir, env)


def collect_item(item: dict, out: Path, repos_toml: Path) -> bool:
    base = (out / item["id"]).resolve()
    meta_path = base / "meta.json"
    if not meta_path.is_file():
        return False
    meta = read_json(meta_path)
    pending = meta.get("pending_run_dir")
    if not pending:
        return False
    ledger = read_json(Path(pending) / "ledger.json")
    last = ledger["rounds"][-1]["no"]
    if not (Path(pending) / f"round-{last}" / "apply.json").is_file():
        write_json(meta_path, {**meta, "incomplete": True})
        return False
    repo = Path(item["repo"]).resolve()
    wt = (base / "wt").resolve()
    finish_item(base, repo, wt, Path(pending), base_env(base / "repos.toml"))
    meta.pop("pending_run_dir")
    meta.pop("incomplete", None)
    write_json(meta_path, meta)
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="소급 재생: 표본 지적을 그 시점 커밋에서 dry-run으로 다시 돌린다")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fake-results", help="<id>/stage1.json(·adj.json·checker.json)을 두는 폴더. 모델을 부르지 않는다")
    parser.add_argument("--repos-toml", default=os.environ.get("CASCADE_REPOS_TOML") or str(SKILL_ROOT / "repos.toml"))
    parser.add_argument("--collect", action="store_true", help="모델을 부르는 실제 실행이 끝난 항목의 원장을 모으고 정리한다")
    args = parser.parse_args(argv)
    items = load_sample(args.sample)[: args.limit]
    out, toml = Path(args.out), Path(args.repos_toml)
    out.mkdir(parents=True, exist_ok=True)
    fake = Path(args.fake_results).resolve() if args.fake_results else None
    failed = 0
    for item in items:
        try:
            if args.collect:
                print(f"{item['id']}: {'수집' if collect_item(item, out, toml) else '건너뜀(대기 중이 아니거나 apply 전)'}")
            else:
                run_item(item, out, toml, fake)
                print(f"{item['id']}: 완료" if fake else "")
        except Exception as e:
            failed += 1
            print(f"{item['id']}: 오류 — {str(e).splitlines()[0] if str(e) else type(e).__name__}")
    print(f"항목 {len(items)}건, 오류 {failed}건")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
