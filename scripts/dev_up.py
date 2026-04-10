#!/usr/bin/env python3
"""scripts/dev_up.py — one-command launcher for the real-Slack onboarding demo.

What this does:
  1. Loads Slack app creds from `bridge/.env.local` (prompts + saves on first run)
  2. Starts ngrok in the background, captures the public HTTPS URL
  3. Compares the URL against `bridge/slack_manifest.json`'s embedded
     hostname; if they differ, rewrites the manifest file AND prints the
     two URLs you need to paste into api.slack.com (or auto-updates via
     `apps.manifest.update` if you've set SLACK_CONFIG_ACCESS_TOKEN)
  4. Starts agent + bridge + onboarding in **production mode** (no
     LOCAL_DEV, no AGENT_LOCAL_STORES — so the OAuth callback writes to
     real DynamoDB + Secrets Manager and the agent reads from real DDB)
  5. Waits for all three services to be healthy
  6. Opens http://localhost:3000/ in your default browser
  7. Tails the bridge log (so you can see what's happening as you click)
  8. Cleans up everything (including ngrok) on Ctrl+C

Required:
  - ngrok installed and configured (`ngrok config check`)
  - AWS creds active (`aws sts get-caller-identity` works)
  - Bedrock model access for Sonnet 4.6 in us-west-2
  - bridge/.venv and onboarding/node_modules already set up
  - The shared Slack app already registered (week 2)
  - The data layer deployed (`infra/data && npm run deploy` — week 1+2)

First-run setup:
  The script prompts for SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, and
  SLACK_SIGNING_SECRET, and saves them to `bridge/.env.local`. After
  that the script reads them silently.

Usage:
  scripts/dev-up.sh                    # full real-Slack flow
  scripts/dev-up.sh --no-browser       # skip the auto-open
  scripts/dev-up.sh --reuse-ngrok      # don't start ngrok; assume one is running
  scripts/dev-up.sh --no-slack         # skip ngrok + Slack flow entirely
                                       # (just runs the 3 services for direct URL access)
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

# ----------------------------------------------------------------------------
# Paths & constants
# ----------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = REPO_ROOT / "bridge"
AGENT_DIR = REPO_ROOT / "coreAgent"
ONBOARDING_DIR = REPO_ROOT / "onboarding"
LOG_DIR = REPO_ROOT / ".smoke-logs"

BRIDGE_ENV_FILE = BRIDGE_DIR / ".env.local"
SLACK_MANIFEST = BRIDGE_DIR / "slack_manifest.json"

AGENT_PORT = 8080
BRIDGE_PORT = 8000
ONBOARDING_PORT = 3000
NGROK_API_PORT = 4040

BRIDGE_URL = f"http://localhost:{BRIDGE_PORT}"
ONBOARDING_URL = f"http://localhost:{ONBOARDING_PORT}"
AGENT_URL = f"http://localhost:{AGENT_PORT}"

REQUIRED_SLACK_VARS = (
    "SLACK_CLIENT_ID",
    "SLACK_CLIENT_SECRET",
    "SLACK_SIGNING_SECRET",
)


# ----------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------

class Color:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def step(msg: str) -> None:
    print(f"\n{Color.BOLD}{Color.BLUE}▶ {msg}{Color.RESET}")


def ok(msg: str) -> None:
    print(f"  {Color.GREEN}✓{Color.RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {Color.RED}✗ {msg}{Color.RESET}")


def warn(msg: str) -> None:
    print(f"  {Color.YELLOW}!{Color.RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {Color.GREY}{msg}{Color.RESET}")


def banner(title: str, lines: list[str], color: str = Color.CYAN) -> None:
    width = max(len(title), max((len(l) for l in lines), default=0)) + 4
    bar = "━" * width
    print(f"\n{color}{Color.BOLD}{bar}{Color.RESET}")
    print(f"{color}{Color.BOLD}  {title}{Color.RESET}")
    print(f"{color}{Color.BOLD}{bar}{Color.RESET}")
    for line in lines:
        print(f"  {line}")
    print(f"{color}{Color.BOLD}{bar}{Color.RESET}")


# ----------------------------------------------------------------------------
# Env file load + save
# ----------------------------------------------------------------------------

def load_env_file(path: Path) -> dict[str, str]:
    """Read a simple KEY=value env file. No fancy quoting / interpolation."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes if present
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def save_env_file(path: Path, env: dict[str, str]) -> None:
    """Write a KEY=value env file with a header."""
    lines = [
        "# bridge/.env.local — created by scripts/dev-up.sh",
        "# Slack app credentials + a stable HMAC secret for onboarding sessions.",
        "# DO NOT COMMIT — already in .gitignore via the bridge/ tree.",
        "",
    ]
    for key in sorted(env.keys()):
        value = env[key].replace('"', '\\"')
        lines.append(f'{key}="{value}"')
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def ensure_slack_creds(require_slack: bool) -> dict[str, str]:
    """Find the Slack creds. Order of precedence:
       1. Process env (already set)
       2. bridge/.env.local
       3. Interactive prompt + save to bridge/.env.local

    If `require_slack` is False, missing Slack creds are tolerated (the
    bridge doesn't need them at startup, only at OAuth callback time).

    Always sets BRIDGE_OAUTH_STATE_SECRET to a stable value (random on
    first run, persisted in bridge/.env.local for subsequent runs)."""
    step("Loading credentials")

    file_env = load_env_file(BRIDGE_ENV_FILE)
    if file_env:
        info(f"loaded {len(file_env)} vars from {BRIDGE_ENV_FILE.relative_to(REPO_ROOT)}")

    creds: dict[str, str] = {}
    missing: list[str] = []
    for var in REQUIRED_SLACK_VARS:
        value = os.environ.get(var) or file_env.get(var, "")
        if value:
            creds[var] = value
        else:
            missing.append(var)

    if missing and require_slack:
        warn(f"missing: {', '.join(missing)}")
        info("First-run setup. These come from api.slack.com → Your App → Basic Information.")
        info("They get saved to bridge/.env.local (gitignored, mode 0600) for next time.")
        if not sys.stdin.isatty():
            fail("can't prompt for credentials — stdin is not a TTY.")
            info("Run `scripts/dev-up.sh` from a real terminal, or pre-populate bridge/.env.local.")
            sys.exit(1)
        for var in missing:
            label = var.replace("SLACK_", "").replace("_", " ").title()
            value = getpass.getpass(f"  Paste {label}: ").strip()
            if not value:
                fail(f"{var} required, aborting")
                sys.exit(1)
            creds[var] = value
    elif missing and not require_slack:
        info(f"--no-slack: skipping Slack cred prompt (missing: {', '.join(missing)})")

    # Ensure a stable HMAC secret across runs (the onboarding UI needs
    # this to verify any session token; even --no-slack mode benefits
    # from a persisted secret so dev-up restarts don't invalidate
    # cookies set on a previous run).
    secret_var = "BRIDGE_OAUTH_STATE_SECRET"
    state_secret = (
        os.environ.get(secret_var) or file_env.get(secret_var, "")
    )
    if not state_secret:
        state_secret = secrets.token_hex(32)
        info(f"generated new {secret_var} (will persist in {BRIDGE_ENV_FILE.name})")
    creds[secret_var] = state_secret

    # Persist if we got anything new (e.g. a freshly-generated secret).
    persisted = {**file_env, **creds}
    if persisted != file_env:
        BRIDGE_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_env_file(BRIDGE_ENV_FILE, persisted)
        ok(f"saved {len(persisted)} vars to {BRIDGE_ENV_FILE.relative_to(REPO_ROOT)}")
    elif not missing:
        ok("all credentials present")
    else:
        ok("state secret loaded (Slack creds deferred)")

    return creds


# ----------------------------------------------------------------------------
# Pre-flight
# ----------------------------------------------------------------------------

def preflight(use_ngrok: bool) -> None:
    step("Pre-flight checks")
    problems: list[str] = []

    if not (BRIDGE_DIR / ".venv" / "bin" / "python").exists():
        problems.append(f"{BRIDGE_DIR}/.venv missing — run `cd bridge && uv sync`")

    if not (ONBOARDING_DIR / "node_modules").exists():
        problems.append(
            f"{ONBOARDING_DIR}/node_modules missing — run `cd onboarding && npm install`"
        )

    if not shutil.which("agentcore"):
        problems.append("`agentcore` CLI not on PATH (needed to start the agent)")

    if use_ngrok and not shutil.which("ngrok"):
        problems.append("ngrok not installed — `brew install ngrok` (or pass --no-slack)")

    for port, name in [
        (AGENT_PORT, "agent"),
        (BRIDGE_PORT, "bridge"),
        (ONBOARDING_PORT, "onboarding"),
    ]:
        if port_in_use(port):
            problems.append(
                f"port {port} ({name}) is in use — "
                f"`lsof -ti:{port} | xargs kill -9` to clear"
            )

    if use_ngrok and port_in_use(NGROK_API_PORT):
        warn(
            f"port {NGROK_API_PORT} is in use — assuming an ngrok instance is "
            f"already running (will read its tunnels). Pass --reuse-ngrok to "
            f"silence this."
        )

    # Just verify creds work; the bridge will fail loud if they don't.
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            problems.append(
                "`aws sts get-caller-identity` failed — "
                "AWS credentials not active (needed for real DDB + Secrets Manager)"
            )
    except FileNotFoundError:
        problems.append("aws CLI not on PATH")
    except subprocess.TimeoutExpired:
        problems.append("`aws sts get-caller-identity` hung — check your network")

    for problem in problems:
        fail(problem)
    if problems:
        sys.exit(1)
    ok("environment ready")


def port_in_use(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


# ----------------------------------------------------------------------------
# ngrok lifecycle + URL detection
# ----------------------------------------------------------------------------

@dataclass
class NgrokTunnel:
    public_url: str
    process: subprocess.Popen | None  # None if --reuse-ngrok


def start_ngrok(reuse: bool) -> NgrokTunnel:
    step("Starting ngrok tunnel")
    LOG_DIR.mkdir(exist_ok=True)

    process: subprocess.Popen | None = None
    if not reuse:
        log_path = LOG_DIR / "ngrok.log"
        info(f"ngrok http {BRIDGE_PORT} (logs → {log_path.relative_to(REPO_ROOT)})")
        process = subprocess.Popen(
            ["ngrok", "http", str(BRIDGE_PORT), "--log=stdout"],
            stdin=subprocess.DEVNULL,
            stdout=open(log_path, "wb"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # Poll the local ngrok API for the tunnel URL.
    deadline = time.monotonic() + 10.0
    public_url: str | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=1.0) as client:
                r = client.get(f"http://127.0.0.1:{NGROK_API_PORT}/api/tunnels")
            if r.status_code == 200:
                tunnels = r.json().get("tunnels", [])
                # Prefer https tunnel pointing at our bridge port.
                for t in tunnels:
                    if (
                        t.get("proto") == "https"
                        and str(BRIDGE_PORT) in t.get("config", {}).get("addr", "")
                    ):
                        public_url = t.get("public_url")
                        break
                if public_url:
                    break
        except Exception:
            pass
        time.sleep(0.3)

    if not public_url:
        if process and process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        fail(f"could not read tunnel URL from ngrok API at :{NGROK_API_PORT}")
        if process:
            log_path = LOG_DIR / "ngrok.log"
            if log_path.exists():
                info(f"ngrok log tail:")
                for line in log_path.read_text().splitlines()[-10:]:
                    info(f"  {line}")
        sys.exit(1)

    ok(f"public URL: {Color.CYAN}{public_url}{Color.RESET}")
    return NgrokTunnel(public_url=public_url, process=process)


def stop_ngrok(tunnel: NgrokTunnel) -> None:
    if tunnel.process is None or tunnel.process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(tunnel.process.pid), signal.SIGTERM)
        tunnel.process.wait(timeout=3)
        ok("ngrok stopped")
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(tunnel.process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


# ----------------------------------------------------------------------------
# Slack manifest sync
# ----------------------------------------------------------------------------

_HOST_RE = re.compile(r"https://[^/]+")


def current_manifest_host() -> str | None:
    """Read the hostname embedded in slack_manifest.json's redirect_urls."""
    if not SLACK_MANIFEST.exists():
        return None
    manifest = json.loads(SLACK_MANIFEST.read_text())
    redirects = manifest.get("oauth_config", {}).get("redirect_urls", [])
    if not redirects:
        return None
    m = _HOST_RE.match(redirects[0])
    return m.group(0) if m else None


def update_manifest_host(new_base: str) -> tuple[str, str]:
    """Rewrite both URLs in slack_manifest.json with `new_base`.

    Returns (redirect_url, events_url) for display.
    """
    manifest = json.loads(SLACK_MANIFEST.read_text())
    redirect_url = f"{new_base}/slack/oauth/callback"
    events_url = f"{new_base}/slack/events"
    manifest.setdefault("oauth_config", {})["redirect_urls"] = [redirect_url]
    manifest.setdefault("settings", {}).setdefault("event_subscriptions", {})[
        "request_url"
    ] = events_url
    SLACK_MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n")
    return redirect_url, events_url


def sync_slack_manifest(public_url: str) -> tuple[str, str]:
    """Sync the local manifest file with the current ngrok URL.

    Always rewrites the local file (even if no change is needed) so the
    file stays the source of truth. Returns the (redirect, events) URLs
    so the caller can print them.
    """
    step("Syncing Slack app config")
    current_host = current_manifest_host()
    redirect_url, events_url = update_manifest_host(public_url)

    if current_host == public_url:
        ok(f"manifest already points at {public_url} — no Slack app config update needed")
        return redirect_url, events_url

    if current_host:
        info(f"old hostname in manifest: {current_host}")
    info(f"new hostname:                 {public_url}")
    ok(f"rewrote {SLACK_MANIFEST.relative_to(REPO_ROOT)} with new URLs")

    banner(
        "ACTION REQUIRED — update the Slack app config",
        [
            f"{Color.BOLD}1.{Color.RESET} Go to: {Color.CYAN}https://api.slack.com/apps{Color.RESET}",
            f"{Color.BOLD}2.{Color.RESET} Open your agent-core app",
            "",
            f"{Color.BOLD}3.{Color.RESET} {Color.BOLD}OAuth & Permissions{Color.RESET} → Redirect URLs → replace with:",
            f"     {Color.GREEN}{redirect_url}{Color.RESET}",
            "",
            f"{Color.BOLD}4.{Color.RESET} {Color.BOLD}Event Subscriptions{Color.RESET} → Request URL → replace with:",
            f"     {Color.GREEN}{events_url}{Color.RESET}",
            f"     {Color.GREY}(Slack will ping it for verification — make sure the bridge is up.){Color.RESET}",
            "",
            f"{Color.BOLD}5.{Color.RESET} Save changes in both pages",
            "",
            f"{Color.GREY}Tip: the bridge is already running by the time you do step 4,{Color.RESET}",
            f"{Color.GREY}so the URL verification will succeed immediately.{Color.RESET}",
        ],
        color=Color.YELLOW,
    )
    print()
    try:
        input(f"  {Color.BOLD}Press Enter once you've saved both fields...{Color.RESET} ")
    except EOFError:
        # No TTY (CI etc.)
        warn("no stdin available, continuing without confirmation")
    return redirect_url, events_url


# ----------------------------------------------------------------------------
# Service startup (production mode)
# ----------------------------------------------------------------------------

@dataclass
class Service:
    name: str
    process: subprocess.Popen
    log_path: Path


def start_services(
    creds: dict[str, str], public_url: str | None
) -> list[Service]:
    step("Starting services (production mode)")
    LOG_DIR.mkdir(exist_ok=True)
    services: list[Service] = []

    base_env = os.environ.copy()
    # Ensure the agent-side flag is unset so the agent talks to real DDB.
    base_env.pop("AGENT_LOCAL_STORES", None)
    # Ensure the bridge-side flag is unset so OAuth callback writes to real DDB.
    base_env.pop("LOCAL_DEV", None)

    # ----- Agent -------------------------------------------------------------
    agent_log = LOG_DIR / "agent.log"
    info(f"agent: agentcore dev --logs (real DDB / Bedrock)  →  {agent_log.relative_to(REPO_ROOT)}")
    agent_proc = subprocess.Popen(
        ["agentcore", "dev", "--logs"],
        cwd=str(AGENT_DIR),
        env=base_env,
        stdin=subprocess.DEVNULL,
        stdout=open(agent_log, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    services.append(Service("agent", agent_proc, agent_log))

    # ----- Bridge ------------------------------------------------------------
    bridge_log = LOG_DIR / "bridge.log"
    bridge_env = base_env.copy()
    bridge_env.update(creds)
    bridge_env["LOCAL_AGENT_URL"] = AGENT_URL  # talk to local agentcore dev
    bridge_env["ONBOARDING_BASE_URL"] = ONBOARDING_URL
    if public_url:
        bridge_env["SLACK_REDIRECT_URI"] = f"{public_url}/slack/oauth/callback"
        # Week 4 chunk A: the bridge's JWT issuer URL must match the
        # public origin so the Gateway's CUSTOM_JWT authorizer can
        # verify tokens against the publicly-reachable JWKS endpoint.
        bridge_env["BRIDGE_PUBLIC_URL"] = public_url

    # Week 4 chunk G: dev_up runs without LOCAL_DEV (production-DDB mode),
    # so gateway_jwt.py requires an explicit RSA private key. Generate a
    # session-stable one and pass it. The key lives only in this process's
    # memory — it's never written to disk.
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    from cryptography.hazmat.primitives import serialization as _ser
    _jwt_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bridge_env["BRIDGE_GATEWAY_JWT_PRIVATE_KEY_PEM"] = _jwt_key.private_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PrivateFormat.PKCS8,
        encryption_algorithm=_ser.NoEncryption(),
    ).decode("ascii")
    info(f"bridge: uvicorn :{BRIDGE_PORT} (real DDB + Secrets Manager)  →  {bridge_log.relative_to(REPO_ROOT)}")
    bridge_proc = subprocess.Popen(
        [str(BRIDGE_DIR / ".venv" / "bin" / "uvicorn"),
         "bridge.main:app", "--port", str(BRIDGE_PORT)],
        cwd=str(BRIDGE_DIR),
        env=bridge_env,
        stdin=subprocess.DEVNULL,
        stdout=open(bridge_log, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    services.append(Service("bridge", bridge_proc, bridge_log))

    # ----- Onboarding --------------------------------------------------------
    onboarding_log = LOG_DIR / "onboarding.log"
    onboarding_env = base_env.copy()
    onboarding_env.update({
        "BRIDGE_URL": BRIDGE_URL,
        "BRIDGE_OAUTH_STATE_SECRET": creds["BRIDGE_OAUTH_STATE_SECRET"],
        "NEXT_PUBLIC_BRIDGE_INSTALL_URL": f"{BRIDGE_URL}/slack/install",
        "PORT": str(ONBOARDING_PORT),
        "NEXT_TELEMETRY_DISABLED": "1",
    })
    info(f"onboarding: npm run dev (Next.js :{ONBOARDING_PORT})  →  {onboarding_log.relative_to(REPO_ROOT)}")
    onboarding_proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=str(ONBOARDING_DIR),
        env=onboarding_env,
        stdin=subprocess.DEVNULL,
        stdout=open(onboarding_log, "wb"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    services.append(Service("onboarding", onboarding_proc, onboarding_log))

    return services


def wait_for_services(timeout: float = 60.0) -> None:
    step("Waiting for services to be healthy")
    targets = {
        "bridge": f"{BRIDGE_URL}/healthz",
        "onboarding": f"{ONBOARDING_URL}/",
        "agent": f"{AGENT_URL}/ping",
    }
    deadline = time.monotonic() + timeout

    while targets and time.monotonic() < deadline:
        for name, url in list(targets.items()):
            try:
                with httpx.Client(timeout=2.0) as client:
                    r = client.get(url)
                if r.status_code < 500:
                    ok(f"{name} healthy")
                    del targets[name]
            except Exception:
                pass
        if targets:
            time.sleep(0.5)

    if targets:
        for name in targets:
            fail(f"{name} did not become healthy in {timeout:.0f}s")
        raise SystemExit(1)


def stop_services(services: list[Service]) -> None:
    if not services:
        return
    step("Stopping services")
    for s in services:
        if s.process.poll() is None:
            try:
                os.killpg(os.getpgid(s.process.pid), signal.SIGTERM)
                ok(f"sent SIGTERM to {s.name}")
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
                warn(f"{s.name} didn't stop on SIGTERM, sent SIGKILL")
            except ProcessLookupError:
                pass


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Launch the full agent-core stack for a real-Slack onboarding demo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't auto-open http://localhost:3000/ in the browser.",
    )
    parser.add_argument(
        "--reuse-ngrok",
        action="store_true",
        help="Don't start ngrok; assume one is already running on :4040.",
    )
    parser.add_argument(
        "--no-slack",
        action="store_true",
        help="Skip ngrok + Slack flow entirely. Useful if you just want the "
             "3 services up to poke at the onboarding URL directly.",
    )
    args = parser.parse_args()

    print(f"{Color.BOLD}agent-core dev launcher{Color.RESET}")
    print(f"{Color.GREY}repo: {REPO_ROOT}{Color.RESET}")

    use_ngrok = not args.no_slack
    preflight(use_ngrok=use_ngrok)
    creds = ensure_slack_creds(require_slack=use_ngrok)

    tunnel: NgrokTunnel | None = None
    services: list[Service] = []

    try:
        if use_ngrok:
            tunnel = start_ngrok(reuse=args.reuse_ngrok)

        # Start services BEFORE the manifest-update prompt so that Slack's
        # URL verification (which fires when the user pastes the new
        # request URL) hits a live bridge immediately.
        services = start_services(
            creds, public_url=tunnel.public_url if tunnel else None
        )
        wait_for_services()

        if use_ngrok and tunnel is not None:
            sync_slack_manifest(tunnel.public_url)

        # ----- Final call-to-action ------------------------------------------
        action_lines = [
            f"{Color.BOLD}Open this in your browser:{Color.RESET}",
            f"     {Color.CYAN}{Color.BOLD}{ONBOARDING_URL}/{Color.RESET}",
            "",
        ]
        if use_ngrok:
            action_lines.extend([
                f"{Color.BOLD}Then:{Color.RESET}",
                f"     1. Click {Color.BOLD}\"Add to Slack\"{Color.RESET}",
                "     2. Authorize in your Slack workspace",
                "     3. You'll land on the config page — edit the system prompt",
                "     4. In Slack: /invite @agent-core, then @agent-core hi",
                "",
                f"{Color.BOLD}Watch the bridge log:{Color.RESET}",
                f"     {Color.GREY}tail -f {(LOG_DIR / 'bridge.log').relative_to(REPO_ROOT)}{Color.RESET}",
                "",
                f"{Color.BOLD}Public URL (Slack → bridge):{Color.RESET}",
                f"     {Color.CYAN}{tunnel.public_url if tunnel else 'n/a'}{Color.RESET}",
            ])
        else:
            action_lines.extend([
                f"{Color.BOLD}--no-slack mode:{Color.RESET}",
                "     no Slack OAuth, no real install. Use --keep-alive on smoke.sh",
                "     to test the onboarding UI directly with a synthetic session token.",
            ])
        action_lines.extend([
            "",
            f"{Color.BOLD}Press Ctrl+C{Color.RESET} to tear everything down.",
        ])
        banner("READY", action_lines, color=Color.GREEN)

        if not args.no_browser:
            try:
                webbrowser.open(f"{ONBOARDING_URL}/")
            except Exception:
                pass

        # Wait for Ctrl+C.
        print()
        try:
            while True:
                # Detect if any service died unexpectedly.
                for s in services:
                    if s.process.poll() is not None:
                        warn(f"{s.name} exited unexpectedly with code {s.process.returncode}")
                        warn(f"  see {s.log_path.relative_to(REPO_ROOT)}")
                        return 1
                time.sleep(1.0)
        except KeyboardInterrupt:
            print()  # newline after ^C

    finally:
        stop_services(services)
        if tunnel is not None:
            stop_ngrok(tunnel)

    return 0


if __name__ == "__main__":
    sys.exit(main())
