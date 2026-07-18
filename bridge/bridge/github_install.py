"""Install-time warm-start for GitHub App onboarding.

After an operator approves a GitHub App ``installation_id`` on the tenant row,
this module verifies that exact binding and seeds the
``codebases`` config so the first Slack message already has a good
shortlist instead of a cold start.

## What warm-start does

1. Mint an installation access token via ``github_app.get_installation_token``.
2. Page through ``GET /installation/repositories`` to list every repo the
   App has access to for this installation.
3. Rank by activity — ``pushed_at`` descending, with ``stargazers_count``
   as a tiebreaker. Most-recently-pushed repo wins.
4. Pick the top 1 as ``default_repo``, the top ``_MAX_BINDINGS`` as the
   ``bindings`` list, and set ``codebases.enabled = True``.
5. Deep-merge the result into the tenant row via ``tenant_write.update_tenant_row``.

## Why pushed_at > stars

The user's codebase bot cares about "what the team is actively working
on," not "what's famous." A 20-star repo with a push 2 hours ago beats
a 2000-star archived repo from 2 years ago for discovery purposes.
Stars is the tiebreaker for the common case where multiple active
repos had pushes in the same day.

## Why NOT size or commit_activity

- ``size``: weakly correlated with relevance. A 40MB monorepo and a 4KB
  config repo are equally likely to be the tenant's main product, so
  size adds noise.
- ``/stats/commit_activity``: more accurate than ``pushed_at`` but
  rate-limited and often returns 202 "computing" on first call, which
  forces retries. Not worth the latency for onboarding.

## Pure vs. IO

``rank_repos`` and ``repos_to_bindings`` are pure and trivially
unit-testable. ``list_installation_repos`` is a thin HTTP wrapper.
``run_install_warm_start`` is the orchestrator that ties it all
together and talks to DynamoDB. Tests should mock ``list_installation_repos``
and exercise the rest against JSON-file fixtures.
"""
from __future__ import annotations

import hmac
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .github_app import get_installation_token, normalize_installation_id
from .tenant_write import (
    GitHubInstallationBindingConflict,
    deep_merge,
    find_tenant_by_github_installation,
    get_tenant_row,
    update_tenant_row,
)

log = logging.getLogger(__name__)


# Cap on how many repos we pre-populate as bindings. The default is
# shown as a shortlist on the onboarding page and in first-use "which
# repo?" prompts, so more than ~5 becomes noise. Power users can add
# more via the onboarding UI after install.
_MAX_BINDINGS = 5

# GitHub's /installation/repositories endpoint accepts page sizes up to
# 100. Most tenants have <100 repos so one page is enough; the
# pagination loop below handles the rare enterprise case.
_REPOS_PER_PAGE = 100

# Hard cap on total repos we'll fetch — we don't want a pathological
# tenant with 10,000 installable repos to stall the warm-start for
# minutes. The top 500 most recently pushed are more than enough to
# pick a good default and a sensible shortlist.
_MAX_REPOS_TO_FETCH = 500

_GITHUB_API_BASE = "https://api.github.com"


@dataclass
class WarmStartResult:
    """Outcome of a warm-start run, returned to the POST endpoint caller."""

    ok: bool
    installation_id: str
    default_repo: str | None = None
    bindings: list[dict[str, Any]] = field(default_factory=list)
    total_repos_available: int = 0
    pending_approval: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# HTTP — list repos via the installation token
# ---------------------------------------------------------------------------

def list_installation_repos(
    installation_token: str,
    *,
    per_page: int = _REPOS_PER_PAGE,
    max_repos: int = _MAX_REPOS_TO_FETCH,
) -> list[dict[str, Any]]:
    """Return the full list of repositories the installation can access.

    Pages through ``GET /installation/repositories`` until the response
    has fewer than ``per_page`` items OR until we've fetched ``max_repos``
    total. Stops early on the hard cap to bound onboarding latency for
    pathologically large installations.

    Raises ``RuntimeError`` on HTTP errors with the GitHub response body
    included for diagnostics.
    """
    if not installation_token:
        raise RuntimeError("installation_token is required")

    repos: list[dict[str, Any]] = []
    page = 1
    while True:
        if len(repos) >= max_repos:
            log.info(
                "github_install: hit max_repos=%s cap, stopping pagination",
                max_repos,
            )
            break

        qs = urllib.parse.urlencode({"per_page": per_page, "page": page})
        url = f"{_GITHUB_API_BASE}/installation/repositories?{qs}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {installation_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "Agent-Bridge/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub list-installation-repos failed: HTTP {e.code}: {body}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"GitHub list-installation-repos failed: network error: {e}"
            ) from e

        page_repos = data.get("repositories", [])
        if not isinstance(page_repos, list):
            raise RuntimeError(
                f"GitHub returned an unexpected payload shape on page {page}: "
                f"{data!r}"
            )
        repos.extend(page_repos)

        # Short page = last page.
        if len(page_repos) < per_page:
            break
        page += 1

    return repos[:max_repos]


# ---------------------------------------------------------------------------
# Ranking (pure)
# ---------------------------------------------------------------------------

def rank_repos(repos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank repos by activity for install-time warm-start.

    Primary key: ``pushed_at`` descending (most recent wins).
    Tiebreaker: ``stargazers_count`` descending.

    Missing/null ``pushed_at`` sorts to the end — a repo with no push
    timestamp is either brand-new with no commits or malformed, and
    either way should not be the tenant's default.

    Pure: does not mutate the input. Returns a new list.
    """
    # Sentinel for missing pushed_at: empty string sorts before any
    # valid ISO8601 string in lexicographic order, so negating by
    # ranking "valid first, then empty" means we key on (has_ts, ts, stars).
    def sort_key(repo: dict[str, Any]) -> tuple[int, str, int]:
        pushed_at = repo.get("pushed_at") or ""
        stars = int(repo.get("stargazers_count") or 0)
        has_timestamp = 1 if pushed_at else 0
        return (has_timestamp, pushed_at, stars)

    # Sort descending on all three: has_timestamp (1 before 0),
    # pushed_at (newer before older — ISO8601 sorts lexicographically),
    # stars (more before fewer).
    return sorted(repos, key=sort_key, reverse=True)


def repos_to_bindings(
    ranked: list[dict[str, Any]],
    limit: int = _MAX_BINDINGS,
) -> list[dict[str, Any]]:
    """Convert a ranked repo list to ``CodebaseBinding`` dicts.

    Shape must match ``CodebaseBindingOut`` / ``CodebaseBinding`` exactly
    (the three-place mirror — see gotcha #14). We take:

      - ``repo``: the ``full_name`` ("owner/name")
      - ``default_branch``: whatever GitHub reports (falls back to "main")
      - ``aliases``: empty — filled in later via onboarding UI or
        ``codebase_affinity`` memory promotion
      - ``channels``: empty — populated when users confirm bindings
        in specific channels

    Drops anything without a ``full_name`` (malformed row).
    """
    bindings: list[dict[str, Any]] = []
    for repo in ranked[:limit]:
        full_name = repo.get("full_name")
        if not full_name:
            continue
        bindings.append(
            {
                "repo": full_name,
                "default_branch": repo.get("default_branch") or "main",
                "aliases": [],
                "channels": [],
            }
        )
    return bindings


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_install_warm_start(
    tenant_id: str,
    installation_id: str,
    region: str,
) -> WarmStartResult:
    """Run the full install-time warm-start for a tenant.

    Steps:
      1. Load the current tenant row and require its operator-approved
         ``github_installation_id`` to match the request exactly.
      2. Mint an installation token and list repos.
      3. Rank and pick default + bindings.
      4. Deep-merge a ``codebases`` block into the row and write back.

    Returns a ``WarmStartResult`` describing the outcome. On failure, the
    tenant row is left untouched. The caller (POST endpoint) should
    translate ``ok=False`` into an HTTP error response and surface
    ``error`` in the payload so the onboarding UI can show it.

    Never raises — every exception is wrapped into ``WarmStartResult``
    so the caller has a single error-handling path.
    """
    try:
        canonical_installation_id = normalize_installation_id(installation_id)
    except (TypeError, ValueError) as e:
        return WarmStartResult(
            ok=False,
            installation_id=str(installation_id),
            error=str(e),
        )
    result = WarmStartResult(
        ok=False,
        installation_id=canonical_installation_id,
    )

    try:
        current = get_tenant_row(tenant_id, region)
    except KeyError:
        result.error = f"tenant {tenant_id!r} does not exist"
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("github_install: unexpected error loading tenant row")
        result.error = f"failed to load tenant row: {e}"
        return result

    approved_installation_id = str(
        (current.get("codebases") or {}).get("github_installation_id") or ""
    )
    if not approved_installation_id:
        result.pending_approval = True
        result.error = (
            "GitHub App installation is not operator-approved for this tenant. "
            "Ask the deployment operator to bind the installation ID first."
        )
        return result
    if not hmac.compare_digest(
        approved_installation_id,
        canonical_installation_id,
    ):
        log.warning(
            "github_install: rejected installation/tenant binding mismatch for tenant=%s",
            tenant_id,
        )
        result.error = "GitHub App installation is not approved for this tenant"
        return result

    try:
        owner = find_tenant_by_github_installation(
            canonical_installation_id,
            region,
        )
    except GitHubInstallationBindingConflict:
        result.error = "GitHub App installation binding is not unique"
        return result
    if owner != tenant_id:
        result.error = "GitHub App installation is not approved for this tenant"
        return result

    try:
        token = get_installation_token(canonical_installation_id)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "github_install: token mint failed for installation_id=%s: %s",
            canonical_installation_id,
            e,
        )
        result.error = f"could not mint installation token: {e}"
        return result

    try:
        repos = list_installation_repos(token)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "github_install: list_installation_repos failed for "
            "installation_id=%s: %s",
            canonical_installation_id,
            e,
        )
        result.error = f"could not list repositories: {e}"
        return result

    result.total_repos_available = len(repos)

    ranked = rank_repos(repos)
    bindings = repos_to_bindings(ranked)
    default_repo = bindings[0]["repo"] if bindings else None

    # Deep-merge the codebases block so anything the onboarding UI has
    # already set on this tenant (e.g., a manual default_repo override)
    # survives the warm-start write. The only field we force is
    # github_installation_id — it already passed the operator-approved
    # exact-match check above.
    codebases_patch = {
        "codebases": {
            "enabled": True,
            "github_installation_id": canonical_installation_id,
            "default_repo": default_repo,
            "bindings": bindings,
            # Don't touch allow_learning — leave whatever's already set.
        }
    }
    merged = deep_merge(current, codebases_patch)

    try:
        update_tenant_row(tenant_id, region, merged)
    except KeyError:
        # Race: tenant deleted between GET and PUT. Extremely unlikely
        # during onboarding but handle cleanly.
        result.error = f"tenant {tenant_id!r} disappeared mid-warm-start"
        return result
    except Exception as e:  # noqa: BLE001
        log.exception("github_install: update_tenant_row failed")
        result.error = f"failed to write tenant row: {e}"
        return result

    result.ok = True
    result.default_repo = default_repo
    result.bindings = bindings
    log.info(
        "github_install: warm-start complete for tenant=%s installation=%s "
        "default_repo=%s bindings=%d total_repos=%d",
        tenant_id,
        canonical_installation_id,
        default_repo,
        len(bindings),
        result.total_repos_available,
    )
    return result
