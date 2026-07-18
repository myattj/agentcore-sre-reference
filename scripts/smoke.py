#!/usr/bin/env python3
"""End-to-end smoke test for the Agent onboarding flow.

What this validates (without touching real Slack):
  - All three services (agent, bridge, onboarding) start cleanly
  - Landing page renders with the correct install URL
  - A bridge-minted session authenticates cookie-gated onboarding pages
    without putting the bearer token in a URL or query string
  - Cookie-gated onboarding and workspace settings pages all render
  - Bridge `/api/tenants/*` GET/PATCH happy paths work
  - Deep-merge: PATCH catalog.allowed_tools preserves catalog.tool_config
  - Negative paths: no auth → 401, bad token → 401, cross-tenant → 403
  - **End-to-end agent integration**: PATCH a distinctive system prompt,
    invoke /debug/message, verify the LIVE Bedrock reply reflects it.
    This is the proof that the onboarding UI is wired to live runtime
    config, without going through Slack.

What this does NOT validate (requires a human + real Slack):
  - The Slack consent screen click on slack.com (Slack requires it)
  - A real @Agent mention in a Slack channel
  - Real `users.conversations` listing (bot must be invited by a human)
  These are covered by `bridge/tests/test_oauth_callback_redirect.py`
  (which mocks Slack) and a 30-second manual demo afterward.

Usage:
  scripts/smoke.sh                   # full run (recommended)
  scripts/smoke.sh --no-services     # if you already started the 3 services
  scripts/smoke.sh --keep-alive      # leave only services running after success
  scripts/smoke.sh --no-agent        # skip the slow Bedrock call (Phase E)
  scripts/smoke.sh -v                # verbose: show all HTTP request/response

Port overrides (useful when a local port is occupied):
  SMOKE_AGENT_PORT=8180 \
  SMOKE_BRIDGE_PORT=8100 \
  SMOKE_ONBOARDING_PORT=3100 scripts/smoke.sh --no-agent

Exit code is 0 on full success, 1 on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# ----------------------------------------------------------------------------
# Paths and constants
# ----------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO_ROOT / "bridge"
AGENT_DIR = REPO_ROOT / "coreAgent"
ONBOARDING_DIR = REPO_ROOT / "onboarding"
EXAMPLES_DIR = REPO_ROOT / "examples"
LOG_DIR = REPO_ROOT / ".smoke-logs"

AGENT_PORT = int(os.environ.get("SMOKE_AGENT_PORT", "8080"))
BRIDGE_PORT = int(os.environ.get("SMOKE_BRIDGE_PORT", "8000"))
ONBOARDING_PORT = int(os.environ.get("SMOKE_ONBOARDING_PORT", "3000"))

BRIDGE_URL = f"http://localhost:{BRIDGE_PORT}"
ONBOARDING_URL = f"http://localhost:{ONBOARDING_PORT}"
AGENT_URL = f"http://localhost:{AGENT_PORT}"

# Use a strong-ish but constant secret so the script is reproducible.
# Bridge and onboarding both pick this up via env var.
SHARED_SECRET = "smoke-test-shared-secret-do-not-use-in-production-32chars"

# Test fixtures.
SMOKE_TENANT_ID = "slack-smoketest"
SMOKE_WORKSPACE_ID = "T_SMOKETEST"


# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------


class Color:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def info(msg: str) -> None:
    print(f"{Color.BLUE}•{Color.RESET} {msg}")


def step(msg: str) -> None:
    print(f"\n{Color.BOLD}{Color.BLUE}▶ {msg}{Color.RESET}")


def ok(msg: str) -> None:
    print(f"  {Color.GREEN}✓{Color.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {Color.RED}✗ {msg}{Color.RESET}")


def warn(msg: str) -> None:
    print(f"  {Color.YELLOW}!{Color.RESET} {msg}")


def grey(msg: str) -> None:
    print(f"  {Color.GREY}{msg}{Color.RESET}")


# ----------------------------------------------------------------------------
# Result tracking
# ----------------------------------------------------------------------------


@dataclass
class Results:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add_pass(self, name: str) -> None:
        self.passed.append(name)
        ok(name)

    def add_fail(self, name: str, detail: str) -> None:
        self.failed.append((name, detail))
        fail(f"{name} — {detail}")

    def add_skip(self, name: str, reason: str) -> None:
        self.skipped.append(name)
        warn(f"{name} — skipped ({reason})")

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed)

    def summary(self) -> None:
        print()
        print(
            f"{Color.BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Color.RESET}"
        )
        if not self.failed:
            print(
                f"{Color.GREEN}{Color.BOLD}✓ {len(self.passed)} passed{Color.RESET}"
                + (
                    f"  {Color.YELLOW}({len(self.skipped)} skipped){Color.RESET}"
                    if self.skipped
                    else ""
                )
            )
        else:
            print(
                f"{Color.RED}{Color.BOLD}✗ {len(self.failed)} failed{Color.RESET}"
                f"  {Color.GREEN}{len(self.passed)} passed{Color.RESET}"
                + (
                    f"  {Color.YELLOW}{len(self.skipped)} skipped{Color.RESET}"
                    if self.skipped
                    else ""
                )
            )
            print()
            for name, detail in self.failed:
                print(f"  {Color.RED}✗{Color.RESET} {name}")
                print(f"    {Color.GREY}{detail}{Color.RESET}")
        print(
            f"{Color.BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Color.RESET}"
        )


# ----------------------------------------------------------------------------
# Pre-flight
# ----------------------------------------------------------------------------


def preflight(no_services: bool) -> None:
    step("Pre-flight checks")
    problems: list[str] = []

    if not (BRIDGE_DIR / ".venv" / "bin" / "python").exists():
        problems.append(
            f"{BRIDGE_DIR}/.venv/bin/python missing — run `cd bridge && uv sync` first"
        )

    if not (ONBOARDING_DIR / "node_modules").exists():
        problems.append(
            f"{ONBOARDING_DIR}/node_modules missing — run `cd onboarding && npm install` first"
        )

    if not no_services:
        for port, name in [
            (AGENT_PORT, "agent"),
            (BRIDGE_PORT, "bridge"),
            (ONBOARDING_PORT, "onboarding"),
        ]:
            if port_in_use(port):
                problems.append(
                    f"port {port} is in use ({name}) — kill the stale process "
                    f"with `lsof -ti:{port} | xargs kill -9` or pass --no-services"
                )

        if not shutil.which("agentcore"):
            problems.append("`agentcore` CLI not on PATH — needed to start the agent")

    for problem in problems:
        fail(problem)
    if problems:
        sys.exit(1)
    ok("env checks passed")


def port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


# ----------------------------------------------------------------------------
# Service lifecycle
# ----------------------------------------------------------------------------


@dataclass
class Service:
    name: str
    process: subprocess.Popen
    log_path: Path


def start_services() -> list[Service]:
    step("Starting services")
    LOG_DIR.mkdir(exist_ok=True)
    services: list[Service] = []

    # ----- Agent -------------------------------------------------------------
    agent_log = LOG_DIR / "agent.log"
    agent_env = os.environ.copy()
    agent_env["AGENT_LOCAL_STORES"] = "1"
    info(
        f"agent: agentcore dev --logs --port {AGENT_PORT} "
        f"(logs → {agent_log.relative_to(REPO_ROOT)})"
    )
    # `--logs` puts agentcore dev in non-interactive mode (the CLI's
    # interactive Ink UI crashes when stdin isn't a TTY).
    agent_proc = subprocess.Popen(
        ["agentcore", "dev", "--logs", "--port", str(AGENT_PORT)],
        cwd=str(AGENT_DIR),
        env=agent_env,
        stdin=subprocess.DEVNULL,
        stdout=open(agent_log, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    services.append(Service("agent", agent_proc, agent_log))

    # ----- Bridge ------------------------------------------------------------
    bridge_log = LOG_DIR / "bridge.log"
    bridge_env = os.environ.copy()
    bridge_env.update(
        {
            "LOCAL_DEV": "1",
            "LOCAL_AGENT_URL": AGENT_URL,
            "BRIDGE_OAUTH_STATE_SECRET": SHARED_SECRET,
            "ONBOARDING_BASE_URL": ONBOARDING_URL,
            # Slack creds aren't needed because we don't exercise OAuth from
            # this script — but uvicorn imports the bridge module which is
            # tolerant of them being unset (only the install/callback paths
            # actually read them).
        }
    )
    info(
        f"bridge: uvicorn bridge.main:app --port {BRIDGE_PORT} (logs → {bridge_log.relative_to(REPO_ROOT)})"
    )
    bridge_proc = subprocess.Popen(
        [
            str(BRIDGE_DIR / ".venv" / "bin" / "uvicorn"),
            "bridge.main:app",
            "--port",
            str(BRIDGE_PORT),
        ],
        cwd=str(BRIDGE_DIR),
        env=bridge_env,
        stdout=open(bridge_log, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    services.append(Service("bridge", bridge_proc, bridge_log))

    # ----- Onboarding --------------------------------------------------------
    onboarding_log = LOG_DIR / "onboarding.log"
    onboarding_env = os.environ.copy()
    onboarding_env.update(
        {
            "BRIDGE_URL": BRIDGE_URL,
            "BRIDGE_OAUTH_STATE_SECRET": SHARED_SECRET,
            "NEXT_PUBLIC_BRIDGE_INSTALL_URL": f"{BRIDGE_URL}/slack/install",
            "PORT": str(ONBOARDING_PORT),
            # Disable telemetry so we don't get the prompt on first run.
            "NEXT_TELEMETRY_DISABLED": "1",
        }
    )
    info(f"onboarding: npm run dev (logs → {onboarding_log.relative_to(REPO_ROOT)})")
    onboarding_proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=str(ONBOARDING_DIR),
        env=onboarding_env,
        stdout=open(onboarding_log, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    services.append(Service("onboarding", onboarding_proc, onboarding_log))

    return services


def wait_for_services(timeout: float = 60.0) -> None:
    step("Waiting for services to be healthy")
    targets = [
        ("bridge", f"{BRIDGE_URL}/healthz"),
        ("onboarding", f"{ONBOARDING_URL}/"),
        # Agent: agentcore dev exposes /ping at the LOCAL_AGENT_URL.
        ("agent", f"{AGENT_URL}/ping"),
    ]
    deadline = time.monotonic() + timeout
    pending = dict(targets)

    while pending and time.monotonic() < deadline:
        for name, url in list(pending.items()):
            try:
                with httpx.Client(timeout=2.0) as client:
                    r = client.get(url)
                if r.status_code < 500:
                    ok(f"{name} healthy ({url})")
                    del pending[name]
            except Exception:
                pass
        if pending:
            time.sleep(0.5)

    if pending:
        for name, url in pending.items():
            fail(f"{name} did not become healthy at {url}")
        raise SystemExit(1)


def stop_services(services: list[Service]) -> None:
    if not services:
        return
    step("Stopping services")
    for s in services:
        if s.process.poll() is None:
            try:
                # Kill the whole process group so we catch agentcore dev's
                # subprocesses + uvicorn's reloader workers + npm's child
                # next-server process.
                os.killpg(os.getpgid(s.process.pid), signal.SIGTERM)
                ok(f"sent SIGTERM to {s.name} (pgid {os.getpgid(s.process.pid)})")
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + 5.0
    for s in services:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            s.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(s.process.pid), signal.SIGKILL)
                warn(f"{s.name} didn't exit on SIGTERM, sent SIGKILL")
            except ProcessLookupError:
                pass


# ----------------------------------------------------------------------------
# Test fixtures (synthetic OAuth — no real Slack)
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class FixtureSnapshot:
    """Exact pre-smoke contents, used to leave the worktree unchanged."""

    tenant_text: str | None
    mapping_text: str | None


def seed_test_tenant() -> FixtureSnapshot:
    """Pre-create the test tenant on disk + workspace mapping.

    This is what the OAuth callback would do for a real install. We
    bypass the OAuth dance entirely because (a) Slack requires a human
    consent click and (b) the OAuth → DDB write path is already covered
    by `bridge/tests/test_oauth_callback_redirect.py`.
    """
    tenant_path = EXAMPLES_DIR / "tenants" / f"{SMOKE_TENANT_ID}.json"
    mapping_path = EXAMPLES_DIR / "workspace_to_tenant.json"
    snapshot = FixtureSnapshot(
        tenant_text=tenant_path.read_text() if tenant_path.exists() else None,
        mapping_text=mapping_path.read_text() if mapping_path.exists() else None,
    )

    config = {
        "tenant_id": SMOKE_TENANT_ID,
        "model_id": "global.anthropic.claude-sonnet-4-6",
        "system_prompt": "You are the smoke-test bot. Be brief.",
        "catalog": {
            "allowed_tools": ["echo"],
            "tool_config": {"echo": {"prefix": "[smoke]"}},
        },
        "byo": {
            "enabled": False,
            "gateway_endpoint": None,
            "gateway_auth": None,
        },
        "memory": {
            "triggers": {
                "message_count": 6,
                "token_count": 1000,
                "idle_timeout_seconds": 1800,
            },
            "namespace": f"tenants/{SMOKE_TENANT_ID}",
            "extraction": {"enabled": True, "rules": ["facts"]},
        },
        "heartbeat": {"busy_threshold": 1, "max_background_seconds": 3600},
    }
    try:
        # Add the workspace mapping so /debug/message can route to this tenant.
        mapping = (
            json.loads(snapshot.mapping_text)
            if snapshot.mapping_text is not None
            else {}
        )
        mapping[SMOKE_WORKSPACE_ID] = SMOKE_TENANT_ID

        tenant_path.write_text(json.dumps(config, indent=2) + "\n")
        mapping_path.write_text(json.dumps(mapping, indent=2) + "\n")
    except Exception:
        restore_test_tenant(snapshot)
        raise

    info(f"seeded test tenant {SMOKE_TENANT_ID} → {tenant_path.relative_to(REPO_ROOT)}")
    info(f"mapped workspace {SMOKE_WORKSPACE_ID} → {SMOKE_TENANT_ID}")
    return snapshot


def restore_test_tenant(snapshot: FixtureSnapshot) -> None:
    """Restore fixtures byte-for-byte, including pre-existing smoke data."""
    tenant_path = EXAMPLES_DIR / "tenants" / f"{SMOKE_TENANT_ID}.json"
    mapping_path = EXAMPLES_DIR / "workspace_to_tenant.json"

    for path, original_text in (
        (tenant_path, snapshot.tenant_text),
        (mapping_path, snapshot.mapping_text),
    ):
        if original_text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original_text)


def mint_session_token(tenant_id: str) -> str:
    """Mint a real session token via the bridge's own helper.

    Sets BRIDGE_OAUTH_STATE_SECRET in the env so `_state_secret()`
    resolves to the same value the bridge process is using.
    """
    os.environ["BRIDGE_OAUTH_STATE_SECRET"] = SHARED_SECRET
    sys.path.insert(0, str(BRIDGE_DIR))
    try:
        from bridge.slack_oauth import make_session_token

        return make_session_token(tenant_id)
    finally:
        sys.path.pop(0)


# ----------------------------------------------------------------------------
# Test phases
# ----------------------------------------------------------------------------


def phase_landing(results: Results, verbose: bool) -> None:
    step("Phase A — landing + error pages")
    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        try:
            r = client.get(f"{ONBOARDING_URL}/")
            assert r.status_code == 200, f"got {r.status_code}"
            assert "Add to Slack" in r.text, (
                "landing page missing 'Add to Slack' button"
            )
            assert f"{BRIDGE_URL}/slack/install" in r.text, (
                "install URL not embedded in landing page"
            )
            results.add_pass("landing page renders with install URL")
        except Exception as e:
            results.add_fail("landing page renders with install URL", str(e))

        try:
            r = client.get(f"{ONBOARDING_URL}/onboarding/error?reason=no_session")
            assert r.status_code == 200
            assert "Session required" in r.text
            results.add_pass("error page renders with reason slug")
        except Exception as e:
            results.add_fail("error page renders with reason slug", str(e))


def _assert_token_not_in_request_url(request: httpx.Request, token: str) -> None:
    """Prove the smoke flow never leaks its bearer token through a URL."""
    requested_url = str(request.url)
    query = request.url.query.decode("utf-8", errors="replace")
    assert token not in requested_url, "session token leaked into requested URL"
    assert token not in query, "session token leaked into requested query string"


def phase_session_cookie(
    results: Results, verbose: bool
) -> tuple[str | None, httpx.Cookies | None]:
    step("Phase B — synthetic authenticated session (no Slack)")

    token = mint_session_token(SMOKE_TENANT_ID)
    if verbose:
        grey("minted tenant session token (redacted)")
    results.add_pass("minted session token via bridge.slack_oauth.make_session_token")

    # Slack OAuth owns the real Set-Cookie response and is regression-tested in
    # bridge/tests/test_oauth_callback_redirect.py. Here we inject the same
    # cookie into the local HTTP client so the smoke test never sends a bearer
    # through a URL.
    with httpx.Client(
        timeout=5.0,
        follow_redirects=False,
        cookies={"tenant_session": token},
    ) as client:
        try:
            r = client.get(
                f"{ONBOARDING_URL}/onboarding/{SMOKE_TENANT_ID}/integrations"
            )
            _assert_token_not_in_request_url(r.request, token)
            assert r.status_code == 200, f"got {r.status_code}"
            assert "Connect your data" in r.text
            results.add_pass(
                "session cookie authenticates without exposing bearer in URL"
            )
        except Exception as e:
            results.add_fail("cookie authentication without URL bearer", str(e))
            return token, None

        cookies = client.cookies
        if verbose:
            grey("tenant_session cookie attached to local smoke client")

    # 1. Use the cookie to fetch the current onboarding pages.
    with httpx.Client(timeout=10.0, follow_redirects=False, cookies=cookies) as client:
        for slug, marker in [
            ("integrations", "Connect your data"),
            ("done", "You&#x27;re ready"),
        ]:
            try:
                r = client.get(f"{ONBOARDING_URL}/onboarding/{SMOKE_TENANT_ID}/{slug}")
                _assert_token_not_in_request_url(r.request, token)
                assert r.status_code == 200
                assert marker in r.text, (
                    f"page marker not found on {slug}: looking for {marker!r}"
                )
                results.add_pass(f"/onboarding/.../{slug} renders")
            except Exception as e:
                results.add_fail(f"/onboarding/.../{slug} renders", str(e))

        # 2. Post-onboarding settings now live under /workspace/{tenantId}.
        for slug, marker in [
            ("", "Workspace overview"),
            ("prompt", "System prompt"),
            ("channels", "Channels"),
            ("skills", "Skills &amp; Runbooks"),
            ("automations", "Automations"),
        ]:
            path = f"/workspace/{SMOKE_TENANT_ID}" + (f"/{slug}" if slug else "")
            try:
                r = client.get(f"{ONBOARDING_URL}{path}")
                _assert_token_not_in_request_url(r.request, token)
                assert r.status_code == 200, f"got {r.status_code}"
                assert marker in r.text, (
                    f"page marker not found on {path}: looking for {marker!r}"
                )
                if slug == "prompt":
                    assert "smoke-test bot" in r.text, (
                        "seeded system_prompt not rendered"
                    )
                results.add_pass(f"{path} renders")
            except Exception as e:
                results.add_fail(f"{path} renders", str(e))

    # 3. Without the cookie, workspace settings should redirect to /error.
    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        try:
            r = client.get(f"{ONBOARDING_URL}/workspace/{SMOKE_TENANT_ID}/prompt")
            _assert_token_not_in_request_url(r.request, token)
            assert r.status_code in (302, 303, 307)
            assert "no_session" in r.headers.get("location", "")
            results.add_pass(
                "no-cookie workspace page redirects to /error?reason=no_session"
            )
        except Exception as e:
            results.add_fail("no-cookie workspace page redirects", str(e))

    return token, cookies


def phase_bridge_api(results: Results, token: str, verbose: bool) -> None:
    step("Phase C — bridge /api/tenants/* direct")
    auth = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=10.0) as client:
        # GET happy path.
        try:
            r = client.get(f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}", headers=auth)
            assert r.status_code == 200, f"got {r.status_code}: {r.text}"
            body = r.json()
            assert body["tenant_id"] == SMOKE_TENANT_ID
            assert body["catalog"]["tool_config"] == {"echo": {"prefix": "[smoke]"}}
            results.add_pass("GET /api/tenants/{id} returns full config")
        except Exception as e:
            results.add_fail("GET /api/tenants/{id}", str(e))

        # PATCH system_prompt should not touch catalog.
        try:
            r = client.patch(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers=auth,
                json={"system_prompt": "Patched by smoke test."},
            )
            assert r.status_code == 200, f"got {r.status_code}: {r.text}"
            body = r.json()
            assert body["system_prompt"] == "Patched by smoke test."
            assert body["catalog"]["tool_config"] == {"echo": {"prefix": "[smoke]"}}
            results.add_pass("PATCH system_prompt preserves catalog (deep merge L1)")
        except Exception as e:
            results.add_fail("PATCH system_prompt preserves catalog", str(e))

        # PATCH catalog.allowed_tools should preserve catalog.tool_config.
        try:
            r = client.patch(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers=auth,
                json={"catalog": {"allowed_tools": ["echo", "start_background_task"]}},
            )
            assert r.status_code == 200, f"got {r.status_code}: {r.text}"
            body = r.json()
            assert body["catalog"]["allowed_tools"] == ["echo", "start_background_task"]
            assert body["catalog"]["tool_config"] == {"echo": {"prefix": "[smoke]"}}, (
                f"deep merge dropped tool_config: {body['catalog']}"
            )
            results.add_pass(
                "PATCH catalog.allowed_tools preserves catalog.tool_config (deep merge L2)"
            )
        except Exception as e:
            results.add_fail(
                "PATCH catalog.allowed_tools preserves tool_config", str(e)
            )

        # 422 on invalid type.
        try:
            r = client.patch(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers=auth,
                json={"catalog": {"allowed_tools": "not a list"}},
            )
            assert r.status_code == 422
            results.add_pass("PATCH invalid type → 422")
        except Exception as e:
            results.add_fail("PATCH invalid type → 422", str(e))

        # 422 on empty system_prompt.
        try:
            r = client.patch(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers=auth,
                json={"system_prompt": ""},
            )
            assert r.status_code == 422
            results.add_pass(
                "PATCH empty system_prompt → 422 (Bedrock validation guard)"
            )
        except Exception as e:
            results.add_fail("PATCH empty system_prompt → 422", str(e))


def phase_negative_paths(results: Results, verbose: bool) -> None:
    step("Phase D — negative paths (auth + isolation)")
    with httpx.Client(timeout=5.0) as client:
        # 1. No auth header.
        try:
            r = client.get(f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}")
            assert r.status_code == 401, f"expected 401, got {r.status_code}"
            results.add_pass("no Authorization header → 401")
        except Exception as e:
            results.add_fail("no auth → 401", str(e))

        # 2. Bad token format.
        try:
            r = client.get(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers={"Authorization": "Bearer not.a.valid.token"},
            )
            assert r.status_code == 401, f"expected 401, got {r.status_code}"
            results.add_pass("garbage token → 401")
        except Exception as e:
            results.add_fail("garbage token → 401", str(e))

        # 3. Cross-tenant: mint a token for slack-other and try to read smoketest.
        other_token = mint_session_token("slack-other")
        try:
            r = client.get(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert r.status_code == 403, f"expected 403, got {r.status_code}"
            results.add_pass("cross-tenant token → 403 (isolation enforced)")
        except Exception as e:
            results.add_fail("cross-tenant → 403", str(e))

        # 4. Tampered cookie via workspace settings (Next.js side).
        with httpx.Client(timeout=5.0, follow_redirects=False) as nc:
            try:
                r = nc.get(
                    f"{ONBOARDING_URL}/workspace/{SMOKE_TENANT_ID}/prompt",
                    cookies={"tenant_session": "tampered.cookie.value.bad"},
                )
                assert r.status_code in (302, 303, 307)
                assert "bad_session" in r.headers.get("location", "")
                results.add_pass("tampered cookie → /error?reason=bad_session")
            except Exception as e:
                results.add_fail("tampered cookie → /error", str(e))


def phase_agent_e2e(results: Results, token: str, verbose: bool) -> None:
    step("Phase E — config → live agent reply (the headline test)")
    info("This calls real Bedrock via /debug/message — may take 5-15s.")

    auth = {"Authorization": f"Bearer {token}"}
    marker = "HAIKUTESTOK"
    new_prompt = (
        f"You must respond with ONLY the single uppercase word "
        f"{marker} and nothing else. No punctuation, no other words."
    )

    # 1. PATCH the system prompt to something distinctive.
    with httpx.Client(timeout=10.0) as client:
        try:
            r = client.patch(
                f"{BRIDGE_URL}/api/tenants/{SMOKE_TENANT_ID}",
                headers=auth,
                json={"system_prompt": new_prompt},
            )
            assert r.status_code == 200, f"got {r.status_code}: {r.text}"
            assert r.json()["system_prompt"] == new_prompt
            results.add_pass("PATCH system_prompt with marker prompt")
        except Exception as e:
            results.add_fail("PATCH marker prompt", str(e))
            return

    # 2. Verify the JSON file on disk reflects the change (proves write
    #    actually flushed, not just cached).
    tenant_path = EXAMPLES_DIR / "tenants" / f"{SMOKE_TENANT_ID}.json"
    try:
        on_disk = json.loads(tenant_path.read_text())
        assert on_disk["system_prompt"] == new_prompt
        assert "echo" in on_disk["catalog"]["allowed_tools"]
        results.add_pass(f"on-disk {tenant_path.name} reflects PATCH")
    except Exception as e:
        results.add_fail("on-disk JSON file updated", str(e))

    # 3. Hit /debug/message — synchronous round-trip through the live agent.
    debug_payload = {
        "workspace_id": SMOKE_WORKSPACE_ID,
        "user_id": "smoke-user",
        "text": "say something",
    }
    try:
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{BRIDGE_URL}/debug/message", json=debug_payload)
        assert r.status_code == 200, f"got {r.status_code}: {r.text}"
        body = r.json()
        reply_text = body.get("text", "")
        if verbose:
            grey(f"agent reply: {reply_text!r}")
        assert reply_text, "agent returned empty reply"
        # The marker should appear in the reply. Models sometimes wrap it
        # ("HAIKUTESTOK." or "Sure: HAIKUTESTOK"), so be lenient.
        assert marker in reply_text.upper(), (
            f"marker {marker!r} not in agent reply: {reply_text!r}"
        )
        results.add_pass(
            "/debug/message reply contains marker — config flowed to live agent"
        )
    except Exception as e:
        results.add_fail("agent reply contains marker", str(e))


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end smoke test for the Agent onboarding flow.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-services",
        action="store_true",
        help="Assume agent + bridge + onboarding are already running on the standard ports.",
    )
    parser.add_argument(
        "--keep-alive",
        action="store_true",
        help=(
            "After a successful run, leave the three service processes running; "
            "synthetic tenant files are still restored."
        ),
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        help="Skip the slow Phase E that calls real Bedrock via /debug/message.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show extra debug output (cookies, agent reply text, etc.)",
    )
    args = parser.parse_args()

    print(f"{Color.BOLD}Agent onboarding smoke test{Color.RESET}")
    print(f"{Color.GREY}repo: {REPO_ROOT}{Color.RESET}")

    preflight(no_services=args.no_services)

    services: list[Service] = []
    results = Results()
    fixture_snapshot: FixtureSnapshot | None = None
    run_completed = False

    try:
        if not args.no_services:
            services = start_services()
            wait_for_services()
        else:
            info("--no-services: assuming the three services are already running")

        fixture_snapshot = seed_test_tenant()

        phase_landing(results, args.verbose)
        token, cookies = phase_session_cookie(results, args.verbose)

        if token is not None:
            phase_bridge_api(results, token, args.verbose)
            phase_negative_paths(results, args.verbose)

            if args.no_agent:
                results.add_skip("Phase E: agent E2E", "--no-agent passed")
            else:
                phase_agent_e2e(results, token, args.verbose)
        else:
            results.add_skip("Phase C-E", "session token minting / cookie flow failed")
        run_completed = True

    finally:
        if fixture_snapshot is not None:
            try:
                restore_test_tenant(fixture_snapshot)
                info("restored test fixtures to their pre-smoke contents")
            except Exception as e:
                results.add_fail("restore test fixtures", str(e))

        results.summary()

        keep_services = args.keep_alive and run_completed and not results.failed
        if services and not keep_services:
            stop_services(services)
        elif services:
            info(
                "--keep-alive: services left running; synthetic tenant files "
                "were restored."
            )
            grey(f"  onboarding: {ONBOARDING_URL}")
            grey(f"  bridge health: {BRIDGE_URL}/healthz")
            grey(f"  agent ping: {AGENT_URL}/ping")
            info("Manual cleanup:")
            process_groups = " ".join(
                f"-{os.getpgid(service.process.pid)}" for service in services
            )
            for s in services:
                grey(f"  {s.name}: process group {os.getpgid(s.process.pid)}")
            grey(f"  kill -TERM -- {process_groups}")
            grey(f"  sleep 5; kill -KILL -- {process_groups} 2>/dev/null || true")

    return 0 if not results.failed else 1


if __name__ == "__main__":
    sys.exit(main())
