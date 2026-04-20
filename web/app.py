"""FastAPI app: GitHub OAuth, repo picker, activity generation."""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

# Project root (github-activity/)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from generate_activity_commits import RunConfig, github_https_clone_url, run_generation

WEB = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(WEB / "templates"))

GITHUB_AUTH = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_API = "https://api.github.com"


def _optional_env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    if v is None or not str(v).strip():
        return default
    return v.strip()


# Session cookies: set SESSION_SECRET in production (e.g. Railway variables). No .env file is read by the web app.
SESSION_SECRET = _optional_env("SESSION_SECRET") or secrets.token_hex(32)

# One GitHub OAuth App for the whole deployment (you register it once). End users each get their own token at login.
# Set these only via the host environment (Railway, export, etc.) — not via a committed .env file.
CLIENT_ID = _optional_env("GITHUB_CLIENT_ID")
CLIENT_SECRET = _optional_env("GITHUB_CLIENT_SECRET")


def oauth_redirect_uri(request: Request) -> str:
    """
    Callback URL must match the authorize request and token exchange exactly.
    Uses proxy headers (Railway, Vercel, etc.). Override with OAUTH_REDIRECT_URI or PUBLIC_APP_URL if needed.
    """
    full = _optional_env("OAUTH_REDIRECT_URI")
    if full:
        return full.rstrip("/")
    base = _optional_env("PUBLIC_APP_URL")
    if base:
        return f"{base.rstrip('/')}/auth/callback"
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return "http://127.0.0.1:8000/auth/callback"
    return f"{proto}://{host}/auth/callback"


app = FastAPI(title="GitHub activity")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=14 * 24 * 3600)

STATIC_DIR = PROJECT_ROOT / "public" / "static"
if not STATIC_DIR.is_dir():
    STATIC_DIR = WEB / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _next_link(link_header: Optional[str]) -> Optional[str]:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


async def github_list_repos(token: str) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    url = (
        f"{GITHUB_API}/user/repos"
        "?per_page=100&affiliation=owner&sort=updated&direction=desc"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        while url:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            batch = r.json()
            if isinstance(batch, list):
                repos.extend(batch)
            url = _next_link(r.headers.get("Link"))
    return repos


async def github_user_profile(token: str) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{GITHUB_API}/user", headers=headers)
        r.raise_for_status()
        return r.json()


async def github_primary_email(token: str) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{GITHUB_API}/user/emails", headers=headers)
        if r.status_code != 200:
            return None
        items = r.json()
        if not isinstance(items, list):
            return None
        for row in items:
            if isinstance(row, dict) and row.get("primary"):
                return row.get("email")
        for row in items:
            if isinstance(row, dict) and row.get("verified"):
                return row.get("email")
        return None


def work_dir_for_web() -> Path:
    """Writable clone directory. Vercel serverless only allows `/tmp`."""
    if os.environ.get("VERCEL"):
        d = Path("/tmp/gh-activity-work")
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = WEB / "data" / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_activity_job(cfg: RunConfig) -> tuple[str, Optional[str]]:
    """Run generation in a worker thread; returns (log text, error or None)."""
    lines: list[str] = []

    def append(line: str) -> None:
        lines.append(line.rstrip())

    try:
        run_generation(cfg, log=append)
        return "\n".join(lines), None
    except Exception as exc:
        return "\n".join(lines), str(exc)


class GenerateBody(BaseModel):
    year: int = Field(..., ge=2000, le=2100)
    repos: list[str] = Field(..., min_length=1, description="owner/repo full names")
    commits_per_repo: int = Field(30, ge=1, le=500)
    dry_run: bool = False
    no_push: bool = False


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "logged_in": bool(request.session.get("access_token")),
            "login": request.session.get("login") or "",
            "oauth_configured": bool(CLIENT_ID and CLIENT_SECRET),
        },
    )


@app.get("/auth/github")
async def oauth_start(request: Request) -> RedirectResponse:
    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="GitHub OAuth is not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET in the server environment.",
        )
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    cb_raw = oauth_redirect_uri(request)
    request.session["oauth_redirect_uri"] = cb_raw
    cb = quote(cb_raw, safe="")
    url = (
        f"{GITHUB_AUTH}?client_id={CLIENT_ID}&redirect_uri={cb}"
        f"&scope=repo&state={state}"
    )
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
async def oauth_callback(
    request: Request,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    code: Optional[str] = None,
    state: Optional[str] = None,
) -> RedirectResponse:
    if error:
        raise HTTPException(
            status_code=400,
            detail=error_description or error,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")
    saved = request.session.get("oauth_state")
    if not saved or saved != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    request.session.pop("oauth_state", None)

    if not CLIENT_ID or not CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="OAuth not configured")

    redirect_uri = request.session.pop("oauth_redirect_uri", None) or oauth_redirect_uri(request)

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            GITHUB_TOKEN,
            headers={
                "Accept": "application/json",
            },
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        r.raise_for_status()
        data = r.json()
    token = data.get("access_token")
    if not token:
        raise HTTPException(status_code=400, detail="No access token from GitHub")

    request.session["access_token"] = token

    profile = await github_user_profile(token)
    request.session["login"] = profile.get("login") or ""
    name = profile.get("name") or request.session["login"]
    email = profile.get("email") or await github_primary_email(token)
    request.session["git_name"] = name
    request.session["git_email"] = email

    return RedirectResponse("/", status_code=302)


@app.get("/auth/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@app.get("/api/repos")
async def api_repos(request: Request) -> dict[str, Any]:
    token = request.session.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in")
    repos = await github_list_repos(token)
    items = []
    for r in repos:
        fn = r.get("full_name")
        if not fn:
            continue
        items.append(
            {
                "full_name": fn,
                "private": bool(r.get("private")),
                "description": (r.get("description") or "")[:200],
                "default_branch": r.get("default_branch") or "main",
                "pushed_at": r.get("pushed_at"),
            }
        )
    return {"repos": items}


@app.post("/api/jobs")
async def create_job(request: Request, body: GenerateBody) -> dict[str, Any]:
    token = request.session.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in")

    if not body.dry_run and not shutil.which("git"):
        raise HTTPException(
            status_code=503,
            detail="The git executable was not found (common on Vercel serverless). "
            "Use Dry run on Vercel to preview only, or deploy with Docker / a VM / Railway / Render with git installed, "
            "or run the CLI on your machine.",
        )

    git_name = request.session.get("git_name") or request.session.get("login") or "User"
    git_email = request.session.get("git_email")
    if not git_email:
        git_email = await github_primary_email(token)
    if not git_email:
        raise HTTPException(
            status_code=400,
            detail="Your GitHub account has no public email; add a verified email on GitHub "
            "or make an email public, then sign out and sign in again.",
        )

    clone_urls: list[str] = []
    for full_name in body.repos:
        full_name = full_name.strip()
        if "/" not in full_name:
            raise HTTPException(status_code=400, detail=f"Invalid repo: {full_name}")
        clone_urls.append(github_https_clone_url(full_name, token))

    cfg = RunConfig(
        year=body.year,
        repos=clone_urls,
        commits_per_repo=body.commits_per_repo,
        work_dir=work_dir_for_web(),
        dry_run=body.dry_run,
        no_push=body.no_push,
        git_author_name=git_name,
        git_author_email=git_email,
    )

    log_text, err = await asyncio.to_thread(run_activity_job, cfg)
    if err:
        return {"status": "error", "log": log_text, "error": err}
    return {"status": "done", "log": log_text, "error": None}
