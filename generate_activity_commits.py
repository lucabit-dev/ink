#!/usr/bin/env python3
"""
Generate backdated, realistic-looking commits on selected GitHub repositories
since a given year. Requires git, network for clone/push, and push credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlparse

# -----------------------------------------------------------------------------
# Realistic commit message templates (conventional-commit style)
# -----------------------------------------------------------------------------

PREFIXES = ("fix", "feat", "chore", "docs", "refactor", "style", "test", "perf")
SCOPES = (
    None,
    "api",
    "auth",
    "build",
    "ci",
    "cli",
    "config",
    "deps",
    "ui",
    "utils",
    "types",
    "tests",
)
SUBJECTS = (
    "adjust error handling",
    "clean up unused imports",
    "clarify wording",
    "tweak typings",
    "small follow-up",
    "handle edge case",
    "reduce noise in logs",
    "align with spec",
    "bump patch version",
    "sync lockfile",
    "narrow type",
    "guard against null",
    "memoize hot path",
    "simplify condition",
    "rename for clarity",
    "extract helper",
    "add missing docstring",
    "fix flaky test",
    "improve DX",
    "address review",
)


def random_commit_message(rng: random.Random) -> str:
    p = rng.choice(PREFIXES)
    scope = rng.choice(SCOPES)
    subj = rng.choice(SUBJECTS)
    if scope:
        return f"{p}({scope}): {subj}"
    return f"{p}: {subj}"


def random_commit_datetime(
    rng: random.Random,
    start: datetime,
    end: datetime,
) -> datetime:
    """Bias toward Tue–Thu, 09:00–19:00 UTC-ish (spread for realism)."""
    span = end - start
    for _ in range(2000):
        offset = rng.random() * span.total_seconds()
        dt = start + timedelta(seconds=offset)
        # Prefer weekdays (Mon=0 .. Sun=6); downweight weekends
        w = dt.weekday()
        if w >= 5 and rng.random() < 0.85:
            continue
        # Prefer working hours
        hour = dt.hour
        if (hour < 7 or hour > 22) and rng.random() < 0.7:
            continue
        # Slight lunch dip
        if 12 <= hour <= 13 and rng.random() < 0.35:
            continue
        return dt.replace(microsecond=rng.randint(0, 999_999))
    # Fallback: uniform
    offset = rng.random() * span.total_seconds()
    return (start + timedelta(seconds=offset)).replace(tzinfo=timezone.utc)


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds")


def run_git(
    cwd: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=merged,
        capture_output=True,
        text=True,
        check=check,
    )


def repo_dir_name(repo_url: str) -> str:
    path = urlparse(repo_url).path.rstrip("/").split("/")
    name = path[-1] if path else "repo"
    if name.endswith(".git"):
        name = name[:-4]
    return name or "repo"


def ensure_clone(repo_url: str, work_root: Path, *, depth: int | None) -> Path:
    name = repo_dir_name(repo_url)
    target = work_root / name
    if target.is_dir() and (target / ".git").is_dir():
        run_git(target, ["remote", "set-url", "origin", repo_url])
        run_git(target, ["fetch", "origin"])
        return target
    clone_args = ["clone", repo_url, str(target)]
    if depth is not None:
        clone_args[1:1] = ["--depth", str(depth)]
    subprocess.run(["git", *clone_args], check=True)
    return target


def current_default_branch(cwd: Path) -> str:
    r = run_git(cwd, ["symbolic-ref", "refs/remotes/origin/HEAD"], check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/")[-1]
    r = run_git(cwd, ["branch", "-r"], check=True)
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("origin/HEAD -> "):
            return line.split("->")[-1].strip().split("/")[-1]
    return "main"


def git_commit_at(
    cwd: Path,
    when: datetime,
    message: str,
    log_path: str,
    rng: random.Random,
    author_identity: tuple[str, str] | None = None,
) -> None:
    log_file = cwd / log_path
    line = f"{when.date().isoformat()}  {rng.randint(1000, 9999)}  activity\n"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line)
    run_git(cwd, ["add", "-f", log_path])
    author = iso_z(when)
    # Occasionally shift committer by 1–3 minutes (amend-style workflow)
    committer_dt = when + timedelta(seconds=rng.randint(0, 180)) if rng.random() < 0.15 else when
    committer = iso_z(committer_dt)
    env = {
        "GIT_AUTHOR_DATE": author,
        "GIT_COMMITTER_DATE": committer,
    }
    if author_identity:
        name, email = author_identity
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = name
        env["GIT_COMMITTER_EMAIL"] = email
    run_git(
        cwd,
        ["commit", "-m", message],
        env=env,
    )


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs into os.environ if the key is not already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        val = rest.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def env_str(key: str) -> str | None:
    v = os.getenv(key)
    return v.strip() if v and v.strip() else None


def env_int(key: str) -> int | None:
    v = env_str(key)
    if v is None:
        return None
    return int(v)


def env_bool(key: str) -> bool:
    v = env_str(key)
    if v is None:
        return False
    return v.lower() in ("1", "true", "yes", "on")


def github_https_clone_url(full_name: str, access_token: str) -> str:
    """
    Build an authenticated HTTPS clone URL for github.com.
    `full_name` is the "owner/repo" string from the GitHub API.
    """
    owner, _, repo = full_name.partition("/")
    if not owner or not repo:
        msg = f"Invalid full_name (expected owner/repo): {full_name!r}"
        raise ValueError(msg)
    tok = quote(access_token, safe="")
    return f"https://x-access-token:{tok}@github.com/{owner}/{repo}.git"


@dataclass
class RunConfig:
    year: int
    repos: list[str]
    commits_per_repo: int = 30
    work_dir: Path | None = None
    branch: str = "github-activity-backfill"
    log_file: str = ".github-activity.log"
    seed: int | None = None
    shallow_clone_depth: int | None = None
    dry_run: bool = False
    no_push: bool = False
    git_author_name: str | None = None
    git_author_email: str | None = None


def run_generation(
    config: RunConfig,
    *,
    log: Callable[[str], None] | None = None,
) -> None:
    """
    Create backdated commits for each clone URL in `config.repos`.
    If `log` is None, lines are printed to stdout.
    """
    out = log or (lambda m: print(m, flush=True))

    if not config.repos:
        raise ValueError("repos must be non-empty")

    now = datetime.now(timezone.utc)
    start = datetime(config.year, 1, 1, tzinfo=timezone.utc)
    if start >= now:
        raise ValueError("year must be before the current instant (UTC)")

    work_root = config.work_dir
    if work_root is None:
        work_root = Path(__file__).resolve().parent / "work"
    if not config.dry_run:
        work_root.mkdir(parents=True, exist_ok=True)

    author_identity: tuple[str, str] | None = None
    if config.git_author_name and config.git_author_email:
        author_identity = (config.git_author_name, config.git_author_email)

    for repo_url in config.repos:
        name = repo_dir_name(repo_url)
        digest = hashlib.sha256(f"{repo_url}\n{config.year}".encode()).hexdigest()
        rng_repo = random.Random((config.seed or 0) ^ int(digest[:8], 16))

        planned_times: list[datetime] = []
        for _ in range(config.commits_per_repo):
            planned_times.append(random_commit_datetime(rng_repo, start, now))
        planned_times.sort()

        out(f"\n=== {name} ({repo_url}) ===")
        if config.dry_run:
            for t in planned_times:
                msg = random_commit_message(rng_repo)
                out(f"  {iso_z(t)}  {msg}")
            continue

        cwd = ensure_clone(repo_url, work_root, depth=config.shallow_clone_depth)
        default_br = current_default_branch(cwd)
        run_git(cwd, ["checkout", default_br])
        run_git(cwd, ["checkout", "-B", config.branch])

        for t in planned_times:
            msg = random_commit_message(rng_repo)
            out(f"  commit @ {iso_z(t)}  {msg}")
            git_commit_at(cwd, t, msg, config.log_file, rng_repo, author_identity=author_identity)

        if not config.no_push:
            out(f"  pushing branch '{config.branch}' to origin...")
            run_git(cwd, ["push", "-u", "origin", config.branch])

    if not config.dry_run:
        out("\nDone. Open a PR from the pushed branch if you use protected main.")


def parse_repos_file(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    load_env_file(script_dir / ".env")

    work_default = env_str("GH_ACTIVITY_WORK_DIR")
    work_path = Path(work_default) if work_default else script_dir / "work"

    ap = argparse.ArgumentParser(
        description="Backfill randomized realistic Git commits since --year on given GitHub repos.",
    )
    ap.add_argument(
        "--year",
        type=int,
        default=env_int("GH_ACTIVITY_YEAR"),
        help="Start year (inclusive); commits are placed between Jan 1 of this year and now. "
        "Can be set via GH_ACTIVITY_YEAR in .env.",
    )
    ap.add_argument(
        "--repos",
        nargs="*",
        default=[],
        help="One or more GitHub clone URLs (https or git@). Or GH_ACTIVITY_REPOS in .env.",
    )
    ap.add_argument(
        "--repos-file",
        type=Path,
        default=None,
        help="Path to a file listing one repo URL per line (# comments allowed). "
        "Or GH_ACTIVITY_REPOS_FILE in .env.",
    )
    ap.add_argument(
        "--commits-per-repo",
        type=int,
        default=env_int("GH_ACTIVITY_COMMITS_PER_REPO") or 30,
        metavar="N",
        help="How many synthetic commits to add per repository (default: 30). "
        "Or GH_ACTIVITY_COMMITS_PER_REPO.",
    )
    ap.add_argument(
        "--work-dir",
        type=Path,
        default=work_path,
        help="Directory to clone/update repositories (default: ./work). Or GH_ACTIVITY_WORK_DIR.",
    )
    ap.add_argument(
        "--branch",
        type=str,
        default=env_str("GH_ACTIVITY_BRANCH") or "github-activity-backfill",
        help="Branch to create/update for synthetic commits (default: github-activity-backfill). "
        "Or GH_ACTIVITY_BRANCH.",
    )
    ap.add_argument(
        "--log-file",
        type=str,
        default=env_str("GH_ACTIVITY_LOG_FILE") or ".github-activity.log",
        help="Tracked file to append lines to (default: .github-activity.log). Or GH_ACTIVITY_LOG_FILE.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=env_int("GH_ACTIVITY_SEED"),
        help="RNG seed for reproducible dates/messages. Or GH_ACTIVITY_SEED.",
    )
    ap.add_argument(
        "--shallow-clone-depth",
        type=int,
        default=env_int("GH_ACTIVITY_SHALLOW_DEPTH"),
        metavar="N",
        help="If set, clone with --depth N (push may be limited; omit for full history). "
        "Or GH_ACTIVITY_SHALLOW_DEPTH.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned commits only; no clone, file writes, or git operations. "
        "Or GH_ACTIVITY_DRY_RUN=1.",
    )
    ap.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Perform real git operations even if GH_ACTIVITY_DRY_RUN is set.",
    )
    ap.add_argument(
        "--no-push",
        action="store_true",
        help="Do not push; only commit locally on the work branch. Or GH_ACTIVITY_NO_PUSH=1.",
    )
    ap.add_argument(
        "--push",
        action="store_true",
        help="Push to origin even if GH_ACTIVITY_NO_PUSH is set.",
    )
    args = ap.parse_args()

    if args.no_dry_run:
        args.dry_run = False
    elif args.dry_run:
        args.dry_run = True
    else:
        args.dry_run = env_bool("GH_ACTIVITY_DRY_RUN")

    if args.push:
        args.no_push = False
    elif args.no_push:
        args.no_push = True
    else:
        args.no_push = env_bool("GH_ACTIVITY_NO_PUSH")

    if args.year is None:
        ap.error("Set --year or GH_ACTIVITY_YEAR in the environment / .env file.")

    repos_env = env_str("GH_ACTIVITY_REPOS")
    repos_from_env = repos_env.split() if repos_env else []

    repos: list[str] = list(args.repos)
    if repos_from_env:
        repos.extend(repos_from_env)

    repos_file = args.repos_file
    if repos_file is None:
        path_s = env_str("GH_ACTIVITY_REPOS_FILE")
        if path_s:
            repos_file = Path(path_s)
    if repos_file:
        repos.extend(parse_repos_file(repos_file))

    if not repos:
        ap.error("Provide --repos and/or --repos-file, or GH_ACTIVITY_REPOS / GH_ACTIVITY_REPOS_FILE.")

    git_name = env_str("GIT_AUTHOR_NAME")
    git_email = env_str("GIT_AUTHOR_EMAIL")
    cfg = RunConfig(
        year=args.year,
        repos=repos,
        commits_per_repo=args.commits_per_repo,
        work_dir=args.work_dir,
        branch=args.branch,
        log_file=args.log_file,
        seed=args.seed,
        shallow_clone_depth=args.shallow_clone_depth,
        dry_run=args.dry_run,
        no_push=args.no_push,
        git_author_name=git_name,
        git_author_email=git_email,
    )
    try:
        run_generation(cfg)
    except ValueError as e:
        ap.error(str(e))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        if e.stderr:
            print(e.stderr, file=sys.stderr)
        if e.stdout:
            print(e.stdout, file=sys.stderr)
        raise SystemExit(e.returncode)
