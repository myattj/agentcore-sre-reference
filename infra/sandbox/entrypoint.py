"""PR sandbox container entrypoint — real Claude agent loop.

Runs a Claude tool-use agent that reads a task description, explores
the cloned repository, makes code changes, and opens a pull request.

Lifecycle (driven by `propose_pr` in coreAgent/tools.py):

    1. ECS spawns this container via `run_task` with one container
       override: `TASK_ID=pr-XXXX`.
    2. Read the matching row from `sandbox_jobs` DDB. The row was
       written by `propose_pr` BEFORE launching this task, so it
       always exists at start. The row carries: tenant_id, repo,
       installation_id, slack_channel_id, slack_thread_id,
       task_description, context_hint.
    3. Mark the row `running`.
    4. Mint a GitHub App installation token via `scm_github`.
    5. git clone the repo over HTTPS using a process-scoped authorization
       header derived from the installation token.
    6. Create a branch `agent/<task_id>`.
    7. Phase 1 (agentic): run the Claude agent loop (agent.py) which
       reads files, plans changes, edits code, and calls submit_changes.
    8. Phase 2 (single call): generate commit message + PR title + body
       from the git diff + task description.
    9. git add + commit + push. Open the PR via GitHub API.
   10. Mark the row `success` with the PR URL (or `error` with the
       failure message if anything blew up). Write agent cost data.
   11. POST to `SANDBOX_CALLBACK_URL` with `Authorization: Bearer
       <SANDBOX_CALLBACK_SECRET>` so the bridge can post the result
       to the originating Slack thread. Even on failure, we POST
       — the bridge needs to surface the error to the user.

Resilience: every step is in a try/except. If anything fails, we
ALWAYS attempt to write the error row + send the callback before
exiting non-zero. The agent's poll loop sees the terminal status
within 5-20 seconds and clears HealthyBusy.

Idempotency / retry: NONE. If this script crashes mid-flight (e.g.
SIGKILL from a Fargate stop), the row stays in `running`. The agent's
poll loop has a hard 10-min ceiling and will mark it `orphaned` and
clear HealthyBusy without leaking it.

Security model: runs as the unprivileged `sandbox` user (set in the
Dockerfile). Has access to ONLY:
  - sandbox_jobs DDB row R/W
  - GitHub App private key (Secrets Manager: agentcore/platform/github_app/*)
  - the callback shared secret (Secrets Manager: agentcore/services/sandbox)
  - Anthropic API key (Secrets Manager: agentcore/platform/anthropic_api_key)
  - own log group writes
No tenant secrets, no audit log, no tenants table. The Claude agent
runs inside this sandbox so the blast radius holds.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3

import scm_github

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("sandbox")

REGION = os.environ.get("AWS_REGION", "us-west-2")
SANDBOX_JOBS_TABLE = os.environ.get("SANDBOX_JOBS_TABLE", "sandbox_jobs")
SANDBOX_CALLBACK_URL = os.environ.get("SANDBOX_CALLBACK_URL", "")
SANDBOX_CALLBACK_SECRET = os.environ.get("SANDBOX_CALLBACK_SECRET", "")
TASK_ID = os.environ.get("TASK_ID", "")

CLONE_DIR = "/tmp/repo"

GITHUB_API = "https://api.github.com"
DEFAULT_GIT_USER_NAME = "Agent Bot"
DEFAULT_GIT_USER_EMAIL = "agent-bot@users.noreply.github.com"
_GITHUB_REPO_SLUG_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"(?P<name>[A-Za-z0-9._-]{1,100})"
)


def validate_github_repo(repo: str) -> str:
    """Return a normalized, exact GitHub ``owner/name`` slug.

    This is duplicated in ``coreAgent/app/coreAgent/tools.py`` because the
    sandbox and agent ship in separate runtimes. Keep both validators in sync.
    """
    if not isinstance(repo, str):
        raise ValueError("repo must be an exact GitHub owner/name slug")
    normalized = repo.strip()
    match = _GITHUB_REPO_SLUG_RE.fullmatch(normalized)
    if (
        match is None
        or "--" in match.group("owner")
        or match.group("name") in {".", ".."}
    ):
        raise ValueError("repo must be an exact GitHub owner/name slug")
    return normalized


def github_repo_url(repo: str) -> str:
    """Build the only Git remote URL the sandbox is allowed to use."""
    return f"https://github.com/{validate_github_repo(repo)}.git"


def get_sandbox_model() -> str:
    """Return the configured direct-Anthropic model ID."""
    from agent import DEFAULT_MODEL

    return os.environ.get("SANDBOX_MODEL", DEFAULT_MODEL)


def get_git_identity() -> tuple[str, str]:
    """Return the repository-local commit identity for sandbox PRs."""
    name = os.environ.get("SANDBOX_GIT_USER_NAME", "").strip() or DEFAULT_GIT_USER_NAME
    email = (
        os.environ.get("SANDBOX_GIT_USER_EMAIL", "").strip() or DEFAULT_GIT_USER_EMAIL
    )
    return name, email


# ---------------------------------------------------------------------------
# DDB helpers
# ---------------------------------------------------------------------------


def _table() -> Any:
    return boto3.resource("dynamodb", region_name=REGION).Table(SANDBOX_JOBS_TABLE)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def get_job(task_id: str) -> dict[str, Any]:
    """Fetch the job row. Raises if missing — `propose_pr` always writes
    the row BEFORE running the task, so missing row means a serious
    coordination bug worth crashing on."""
    resp = _table().get_item(Key={"task_id": task_id})
    item = resp.get("Item")
    if not item:
        raise RuntimeError(
            f"sandbox_jobs row not found for task_id={task_id!r}. The agent "
            "should always write the row before launching the task."
        )
    return item


def update_status(task_id: str, **fields: Any) -> None:
    """UpdateItem with the given fields. Each field becomes a SET clause."""
    if not fields:
        return
    expr_parts: list[str] = []
    expr_names: dict[str, str] = {}
    expr_values: dict[str, Any] = {}
    for i, (key, value) in enumerate(fields.items()):
        placeholder = f"#f{i}"
        value_placeholder = f":v{i}"
        expr_parts.append(f"{placeholder} = {value_placeholder}")
        expr_names[placeholder] = key
        # DynamoDB rejects Python floats — convert to Decimal.
        expr_values[value_placeholder] = (
            Decimal(str(value)) if isinstance(value, float) else value
        )
    _table().update_item(
        Key={"task_id": task_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def run_git(
    args: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Run a git command and raise on non-zero exit. Captures stderr in
    the exception so failures land in CloudWatch with diagnostic context."""
    log.info("git %s", " ".join(args))
    try:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {e.returncode}): {e.stderr.strip()}"
        ) from e


def github_auth_env(token: str) -> dict[str, str]:
    """Build a process-only Git environment for GitHub HTTPS operations.

    The credential stays out of argv, logs, the remote URL, and repository
    configuration. Callers must pass this environment to every networked Git
    command because the header is deliberately not persisted after the
    process exits.
    """
    basic_auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    auth_env = os.environ.copy()
    auth_env.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic_auth}",
        }
    )
    return auth_env


def clone_repo(repo: str, token: str, target_dir: str) -> None:
    """Shallow-clone a repo via HTTPS using the installation token as basic-auth."""
    url = github_repo_url(repo)
    target = Path(target_dir).resolve()
    temporary_root = Path(CLONE_DIR).parent.resolve()
    if target == temporary_root or not target.is_relative_to(temporary_root):
        raise ValueError(
            "clone target must be a dedicated directory beneath the system temp root"
        )
    if target.exists():
        # Stale workspace from a prior run in the same container. Shouldn't
        # happen for one-shot Fargate tasks but cheap to handle.
        shutil.rmtree(target)
    # Pass credentials through Git's per-process config environment. Keeping
    # the token out of argv prevents it from appearing in process listings,
    # command logs, and git error messages.
    run_git(["clone", "--depth", "50", url, str(target)], env=github_auth_env(token))
    # Identify the bot for the commit.
    git_user_name, git_user_email = get_git_identity()
    run_git(["config", "user.name", git_user_name], cwd=str(target))
    run_git(["config", "user.email", git_user_email], cwd=str(target))


def push_branch(
    repo: str, branch: str, token: str, target_dir: str = CLONE_DIR
) -> None:
    """Push to the validated GitHub URL, ignoring mutable repository remotes."""
    url = github_repo_url(repo)
    run_git(
        ["push", url, f"{branch}:{branch}"],
        cwd=target_dir,
        env=github_auth_env(token),
    )


def get_default_branch(repo: str, token: str) -> str:
    """GET /repos/<repo> → default_branch. Used as the PR base."""
    repo = validate_github_repo(repo)
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Agent-Sandbox/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    branch = data.get("default_branch") or "main"
    return branch


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------


def open_pull_request(
    repo: str,
    token: str,
    head: str,
    base: str,
    title: str,
    body: str,
) -> str:
    """POST /repos/<repo>/pulls. Returns the html_url of the new PR."""
    repo = validate_github_repo(repo)
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{repo}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "Agent-Sandbox/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub PR creation failed: HTTP {e.code}: {body_text}"
        ) from e
    pr_url = data.get("html_url", "")
    if not pr_url:
        raise RuntimeError(f"GitHub returned no html_url in PR response: {data!r}")
    return pr_url


# ---------------------------------------------------------------------------
# Callback to bridge
# ---------------------------------------------------------------------------


def _progress_url() -> str:
    """Derive the progress endpoint URL from the completion callback URL.

    The callback URL is e.g. ``https://agent.example.com/internal/sandbox_complete``.
    The progress URL replaces the last path component:
    ``https://agent.example.com/internal/sandbox_progress``.
    """
    if not SANDBOX_CALLBACK_URL:
        return ""
    return SANDBOX_CALLBACK_URL.rsplit("/", 1)[0] + "/sandbox_progress"


def post_progress(task_id: str, step: str) -> None:
    """Report a progress milestone to the bridge for Slack tracker updates.

    Best-effort — failures are logged but never stop the sandbox flow.
    The bridge posts/updates a Block Kit progress bar in the Slack thread.
    """
    url = _progress_url()
    if not url or not SANDBOX_CALLBACK_SECRET:
        return
    payload = json.dumps({"task_id": task_id, "step": step}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {SANDBOX_CALLBACK_SECRET}",
            "Content-Type": "application/json",
            "User-Agent": "Agent-Sandbox/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("progress ok (%s): %s", step, resp.status)
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("progress POST failed (%s): %s", step, e)


def post_callback(task_id: str, status: str, pr_url: str = "", error: str = "") -> None:
    """Notify the bridge so it can post the result to the original Slack
    thread. Best-effort — if the callback fails, the agent's DDB poll
    loop will still observe the terminal status and clear HealthyBusy.
    The Slack post would just be missing."""
    if not SANDBOX_CALLBACK_URL or not SANDBOX_CALLBACK_SECRET:
        log.warning("SANDBOX_CALLBACK_URL/SECRET not set — skipping bridge callback")
        return
    payload = json.dumps(
        {
            "task_id": task_id,
            "status": status,
            "pr_url": pr_url,
            "error": error,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        SANDBOX_CALLBACK_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {SANDBOX_CALLBACK_SECRET}",
            "Content-Type": "application/json",
            "User-Agent": "Agent-Sandbox/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("callback ok: %s", resp.status)
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("callback POST failed: %s", e)


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------


def main() -> int:
    if not TASK_ID:
        log.error("TASK_ID env var is required")
        return 2

    log.info("starting sandbox task task_id=%s", TASK_ID)

    # Fetch the job row.
    try:
        job = get_job(TASK_ID)
    except Exception:  # noqa: BLE001
        log.exception("failed to fetch job row")
        # Can't update DDB if we can't read it; just bail and let the
        # agent's poll loop hit the orphan ceiling.
        return 3

    repo = job.get("repo", "")
    installation_id = job.get("installation_id", "")
    if not repo or not installation_id:
        msg = f"job row missing required fields: repo={repo!r} installation_id={installation_id!r}"
        log.error(msg)
        update_status(TASK_ID, status="error", error=msg, completed_at=_now_iso())
        post_callback(TASK_ID, status="error", error=msg)
        return 4
    try:
        repo = validate_github_repo(repo)
    except ValueError as exc:
        msg = str(exc)
        log.error(msg)
        update_status(TASK_ID, status="error", error=msg, completed_at=_now_iso())
        post_callback(TASK_ID, status="error", error=msg)
        return 4

    # Mark the job running and post the first progress update.
    try:
        update_status(TASK_ID, status="running", started_at=_now_iso())
    except Exception:  # noqa: BLE001
        # Non-fatal: continue with the actual work even if the status
        # write blipped. The completion write at the end is the one
        # that matters.
        log.exception("failed to mark row running (continuing)")

    post_progress(TASK_ID, "started")

    # Run the PR workflow inside one error boundary so any failure still
    # writes a clean error state and posts the callback.
    error_msg = ""
    pr_url = ""
    try:
        # Mint a fresh installation token (don't trust any cached one
        # from a parent process — this container has no parent state).
        token = scm_github.get_installation_token(installation_id)

        # Discover the base branch.
        base_branch = get_default_branch(repo, token)
        log.info("default branch for %s: %s", repo, base_branch)

        # Clone + branch.
        clone_repo(repo, token, CLONE_DIR)
        branch = f"agent/{TASK_ID}"
        run_git(["checkout", "-b", branch], cwd=CLONE_DIR)

        post_progress(TASK_ID, "cloning")

        # ---- Phase 1: agentic editing ----
        # The agent loop reads/edits files in the clone via Claude
        # tool-use. It returns a structured result with the files
        # changed and a summary of the work done.
        from agent import run_agent_loop, generate_pr_metadata

        sandbox_model = get_sandbox_model()
        sandbox_budget = float(os.environ.get("SANDBOX_PR_BUDGET", "5.0"))

        agent_result = run_agent_loop(
            work_dir=CLONE_DIR,
            task_description=job.get("task_description", ""),
            context_hint=job.get("context_hint", ""),
            model=sandbox_model,
            budget_dollars=sandbox_budget,
            progress_callback=lambda step: post_progress(TASK_ID, step),
        )

        # Write cost data to the job row regardless of outcome. The
        # daemon poller reads this to charge sandbox spend against
        # the tenant's monthly counter.
        try:
            update_status(
                TASK_ID,
                agent_model=sandbox_model,
                agent_input_tokens=agent_result.token_budget.input_tokens,
                agent_output_tokens=agent_result.token_budget.output_tokens,
                agent_cost_cents=agent_result.token_budget.cost_cents,
            )
        except Exception:  # noqa: BLE001
            log.exception("failed to write agent cost data (continuing)")

        # Check if the agent actually produced changes.
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=CLONE_DIR,
            capture_output=True,
            text=True,
        )
        has_changes = bool(git_status.stdout.strip())

        if not has_changes:
            raise RuntimeError(
                f"Agent produced no file changes. "
                f"Summary: {agent_result.summary or '(none)'}. "
                f"Error: {agent_result.error or '(none)'}"
            )

        post_progress(TASK_ID, "editing")

        # ---- Phase 2: PR metadata generation ----
        # Single Claude call to generate commit message + PR title +
        # body from the diff. Cheap and fast.
        run_git(["add", "-A"], cwd=CLONE_DIR)
        diff_stat = subprocess.run(
            ["git", "diff", "--cached", "--stat"],
            cwd=CLONE_DIR,
            capture_output=True,
            text=True,
        ).stdout

        pr_meta = generate_pr_metadata(
            task_description=job.get("task_description", ""),
            agent_summary=agent_result.summary,
            diff_stat=diff_stat,
            model=sandbox_model,
        )

        run_git(
            ["commit", "-m", pr_meta.commit_message],
            cwd=CLONE_DIR,
        )

        push_branch(repo, branch, token)

        post_progress(TASK_ID, "pushing")

        # Open the PR.
        pr_url = open_pull_request(
            repo=repo,
            token=token,
            head=branch,
            base=base_branch,
            title=pr_meta.title,
            body=pr_meta.body,
        )
        log.info("opened PR: %s", pr_url)

        post_progress(TASK_ID, "opening_pr")

    except Exception as e:  # noqa: BLE001 — single failure path
        log.exception("propose_pr sandbox flow failed")
        error_msg = f"{type(e).__name__}: {e}"

    # Step N — write terminal status + callback (always runs).
    final_status = "success" if pr_url and not error_msg else "error"
    update_fields = {
        "status": final_status,
        "completed_at": _now_iso(),
    }
    if pr_url:
        update_fields["pr_url"] = pr_url
    if error_msg:
        update_fields["error"] = error_msg

    try:
        update_status(TASK_ID, **update_fields)
    except Exception:  # noqa: BLE001
        log.exception("failed to write terminal status row (continuing to callback)")

    post_callback(TASK_ID, status=final_status, pr_url=pr_url, error=error_msg)

    return 0 if final_status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
