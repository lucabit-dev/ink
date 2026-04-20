# GitHub activity commit generator

`generate_activity_commits.py` creates **backdated**, **realistic-looking** Git commits (weekday/work-hour bias, conventional-commit-style messages) on branches you choose, for repositories you can clone and push to. It does **not** use the GitHub API to invent commits server-side: it runs normal `git commit` with `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE`, then pushes a dedicated branch.

## Web UI

FastAPI app in **`web/`** (`app.py` at repo root re-exports it). **Anyone** who visits your deployment can click **Sign in with GitHub**; each user authorizes your **one** [GitHub OAuth app](https://github.com/settings/developers) and gets their **own** short‑lived token. The app then lists **that user’s** owned repos and runs the generator **as them** (their token, their repos, their year selection).

The web server **does not read `GITHUB_CLIENT_*` from any `.env` file**. Set OAuth and session secrets only via your platform’s environment (e.g. [Railway variables](https://docs.railway.app/develop/variables), Vercel, or `export` for local dev).

**OAuth app setup (once per deployment):**

1. Create a GitHub OAuth app. Under **Authorization callback URL**, add every URL where `/auth/callback` will be used, e.g.:
   - `http://127.0.0.1:8000/auth/callback` (local)
   - `https://<your-service>.up.railway.app/auth/callback`
   - `https://<project>.vercel.app/auth/callback`  
   (GitHub allows multiple callback URLs; wildcards are not supported, so add each deploy URL explicitly.)

2. Set **server** environment variables (not committed files):

   | Variable | Purpose |
   |----------|---------|
   | `GITHUB_CLIENT_ID` | From the OAuth app |
   | `GITHUB_CLIENT_SECRET` | From the OAuth app |
   | `SESSION_SECRET` | Long random string so signed cookies are stable across restarts |
   | `PUBLIC_APP_URL` or `OAUTH_REDIRECT_URI` | **Optional.** If unset, the callback URL is derived from the incoming request (`X-Forwarded-*` headers on Railway/Vercel). Use `PUBLIC_APP_URL=https://your-host` if that detection is wrong. |

3. Run locally:

   ```bash
   export GITHUB_CLIENT_ID=... GITHUB_CLIENT_SECRET=... SESSION_SECRET=...
   pip install -r requirements.txt
   uvicorn app:app --reload --host 127.0.0.1 --port 8000
   ```

4. Open the site, sign in, pick repos and **Activity since year**, then **Run**. `/api/jobs` returns the full log when the run finishes.

The `repo` OAuth scope is required to clone and push. Commits use the signed-in user’s **name** and a **verified email** from GitHub.

Clones live under `web/data/work/` locally. On Vercel (`VERCEL=1`), under `/tmp/gh-activity-work`.

### Deploy to Railway (recommended for real git runs)

The repo includes a **`Dockerfile`** that installs **`git`** and runs Uvicorn on **`PORT`** (Railway sets this).

1. Push this repo to GitHub and [create a Railway project](https://railway.app) from the repo.
2. In **Variables**, add `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, and `SESSION_SECRET`. Optionally set `PUBLIC_APP_URL` to your Railway public URL if OAuth callback detection fails.
3. In your GitHub OAuth app, add the Railway callback URL: `https://<your-domain>/auth/callback`.
4. Deploy. Each visitor signs in with GitHub and runs the tool on **their** repositories only.

A **`Procfile`** is included for Nixpacks-style builds without Docker; the Dockerfile path is preferred when present so `git` is guaranteed.

### Deploy to Vercel

The serverless entry is **`api/index.py`** (see **`vercel.json`** rewrites). Root **`app.py`** is ignored in the Vercel upload so only one Python Function is built; **`Dockerfile`** / **`Procfile`** are also ignored there (they stay in Git for Railway). Static assets live under **`public/static/`**.

Set the same env vars in the Vercel dashboard (no `.env` in repo).

**Limits:** Short timeouts, often **no `git`** in the runtime—**dry run** still works; **full** clone/commit/push usually needs Railway/Docker/self-hosted `uvicorn`.

---

## Prerequisites (CLI)

- **Python 3.10+** (uses only the standard library).
- **Git** installed and on your `PATH`.
- **Network access** to clone/fetch and push.
- **Credentials** for GitHub (SSH keys for `git@github.com:...` URLs, or HTTPS with a credential helper / token).

Only use repos you **own** or are **allowed to push** to. Prefer a branch other than your default (the script defaults to `github-activity-backfill`) and open a PR if `main` is protected.

## Configuration: `.env`

1. Copy the example file and edit it:

   ```bash
   cd /path/to/github-activity
   cp .env.example .env
   ```

2. Set at least:

   - `GH_ACTIVITY_YEAR` — first year of the synthetic range (commits land between **1 Jan that year** and **now**, UTC).
   - Either `GH_ACTIVITY_REPOS` (one or more clone URLs, separated by spaces or newlines) **or** `GH_ACTIVITY_REPOS_FILE` (path to a text file with one URL per line, `#` comments allowed).

3. Optional variables (see comments in `.env.example`): work directory, branch name, commits per repo, log file name, seed, shallow clone depth, dry-run, no-push.

The script loads `.env` from the **same directory as the script** and fills in **unset** environment variables only (your shell already-exported variables win).

**Git author identity:** commits use your Git user from global config unless you set `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL` in `.env` or in the environment.

## Usage

Show all options:

```bash
./generate_activity_commits.py --help
```

**Dry run** — print timestamps and messages only (no clone/commit/push):

```bash
./generate_activity_commits.py --dry-run --year 2023 --repos https://github.com/you/sandbox.git
# or with .env defining GH_ACTIVITY_YEAR and repos
./generate_activity_commits.py --dry-run
```

**Full run** — clone/update under `./work`, create/update the activity branch, commit, push:

```bash
./generate_activity_commits.py --year 2021 \
  --repos git@github.com:you/repo-a.git git@github.com:you/repo-b.git
```

**Use a repo list file**:

```bash
./generate_activity_commits.py --year 2022 --repos-file ./my-repos.txt
```

**Local commits only** (no push):

```bash
./generate_activity_commits.py --no-push --year 2020 --repos https://github.com/you/r.git
```

### CLI vs `.env`

| Setting | CLI flag | Environment variable |
|--------|-----------|-------------------------|
| Start year | `--year` | `GH_ACTIVITY_YEAR` |
| Repo URLs | `--repos` … | `GH_ACTIVITY_REPOS` |
| Repo list file | `--repos-file` | `GH_ACTIVITY_REPOS_FILE` |
| Work directory | `--work-dir` | `GH_ACTIVITY_WORK_DIR` |
| Activity branch | `--branch` | `GH_ACTIVITY_BRANCH` |
| Commits per repo | `--commits-per-repo` | `GH_ACTIVITY_COMMITS_PER_REPO` |
| Log file in repo | `--log-file` | `GH_ACTIVITY_LOG_FILE` |
| RNG seed | `--seed` | `GH_ACTIVITY_SEED` |
| Shallow clone | `--shallow-clone-depth` | `GH_ACTIVITY_SHALLOW_DEPTH` |
| Dry run | `--dry-run` | `GH_ACTIVITY_DRY_RUN` (`1` / `true` / `yes`) |
| Skip push | `--no-push` | `GH_ACTIVITY_NO_PUSH` |

CLI arguments **override** the corresponding environment defaults where noted.

- If `GH_ACTIVITY_DRY_RUN` is set but you want a real run: pass **`--no-dry-run`**.
- If `GH_ACTIVITY_NO_PUSH` is set but you want to push: pass **`--push`**.

## What gets changed in each repo

- Clones (or updates) live under `work/<repo-name>/` by default.
- Creates or resets branch **`github-activity-backfill`** (configurable) from the remote default branch.
- Appends lines to a tracked file (default **`.github-activity.log`**) and commits with historical author dates. The file is added with `git add -f` so it is still committed if similar paths would normally be ignored.

After a successful push, open a pull request from the activity branch if you want those commits on your default branch.

## Troubleshooting

- **Authentication failed** — configure SSH (`ssh -T git@github.com`) or HTTPS credentials before pushing.
- **`--year must be in the past`** — use a year strictly before the current calendar year end relative to “now”.
- **Nothing to commit / empty commit** — rare; ensure the log path is writable and the repository is not in a broken state.
- **Protected branch** — push targets only the configured activity branch; merge via PR when allowed.
