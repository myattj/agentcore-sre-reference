"""Pluggable code-access backend for the code_* catalog tools.

Exposes four operations (search, read_file, find_symbol, list_commits)
behind a ``CodeBackend`` Protocol so the concrete provider can be
swapped without touching the tool surface in ``tools.py``.

## Current implementation: GithubBackend

``GithubBackend`` uses the GitHub App installation token (minted by
``scm_github.get_installation_token``) and hits the public GitHub API
directly:

  - ``search_code``   → ``GET /search/code`` (Code Search API)
  - ``read_file``     → ``GET /repos/{owner}/{repo}/contents/{path}``
  - ``find_symbol``   → ``GET /search/code`` with a symbol-shaped query
  - ``list_commits``  → ``GET /repos/{owner}/{repo}/commits``

## Why GithubBackend first, not Greptile

1. Greptile requires a separate API key + a one-time indexing step per
   repo, both of which are extra onboarding surface we don't have yet.
2. The step 3 warm-start already proves the installation-token path,
   and GitHub Code Search returns results in <1s on a warm index.
3. Shipping the tool interface against the simplest possible backend
   lets us validate the prompt integration + repo-defaulting flow
   end-to-end. Once we have real query logs showing Code Search's
   weaknesses (keyword-only, default-branch-only, 30 QPM), we swap in
   a ``GreptileBackend`` behind the same Protocol.

## Known limitations of GithubBackend (documented so the tool prompts
## can tell the model what it CAN and can't do)

- Code Search indexes **default branches only** — no way to search a
  feature branch. ``search_code`` and ``find_symbol`` ignore ``ref``.
- Rate limit: **30 searches/min per installation**. High-traffic
  tenants will feel this. Backoff is handled at the tool level.
- Index staleness: GitHub's Blackbird backend is usually <1h behind
  default-branch HEAD but occasional lag is real.
- No symbol-AST awareness: ``find_symbol`` uses a lexical
  "symbol in:file" query which has false positives on overloaded
  names. The tool output flags this so the model can double-check.

``read_file`` is NOT subject to the search rate limit and always
serves the exact ref requested.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchHit:
    """One result row from a code search. Backend-agnostic."""
    repo: str  # "owner/name"
    path: str
    html_url: str
    score: float = 0.0
    snippet: str = ""  # a short highlighted excerpt, backend-dependent


@dataclass(frozen=True)
class FileContent:
    """A file read from the backend. ``content`` is always decoded UTF-8 text.

    For binary files, the backend should raise ``RuntimeError`` rather than
    returning garbage — the tool layer translates that into a human message.
    """
    repo: str
    path: str
    ref: str
    sha: str
    size: int
    content: str
    truncated: bool = False  # True when the backend capped output


@dataclass(frozen=True)
class CommitInfo:
    """One commit row from ``list_commits``. Backend-agnostic.

    ``message`` is the first line (subject) of the commit message only —
    full bodies balloon Slack output and are rarely useful at the glance
    "what just shipped" level these tools serve. Callers who need the
    body can read the commit via a separate API.
    """
    repo: str
    sha: str              # full 40-char SHA
    short_sha: str        # first 7 chars, for human display
    author: str           # "Name <email>" when both are present
    date: str             # ISO-8601 from GitHub's committer.date
    message: str          # first line only
    html_url: str


@dataclass
class BackendError(Exception):
    """A wrapped backend failure with a user-facing message."""
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class CodeBackend(Protocol):
    """Stable interface the code_* catalog tools call through.

    Every method takes a ``repo`` slug ("owner/name"). The tool layer
    requires the caller (the LLM) to pass this explicitly — there is
    no silent default.
    """

    def search_code(
        self, query: str, repo: str, *, max_results: int = 20
    ) -> list[SearchHit]: ...

    def read_file(
        self, repo: str, path: str, *, ref: str | None = None
    ) -> FileContent: ...

    def find_symbol(
        self, symbol: str, repo: str, *, max_results: int = 20
    ) -> list[SearchHit]: ...

    def list_commits(
        self,
        repo: str,
        *,
        ref: str | None = None,
        path: str | None = None,
        limit: int = 10,
    ) -> list[CommitInfo]: ...


# ---------------------------------------------------------------------------
# GithubBackend — direct GitHub API using the installation token
# ---------------------------------------------------------------------------

# Hard caps on response sizes so a pathological call doesn't blow up
# the Slack message pipeline. Tools can ask for less but never more.
_FILE_SIZE_HARD_CAP_BYTES = 64 * 1024  # 64 KB — ~1500 lines of code
_SNIPPET_CHAR_LIMIT = 400


class GithubBackend:
    """CodeBackend backed by api.github.com via an installation access token.

    The token is fetched lazily per-call via a caller-supplied
    ``token_provider`` (a zero-arg callable that returns a valid
    installation token). This indirection keeps the backend stateless
    and testable — tests pass a ``lambda: "ghs_fake"``.
    """

    def __init__(
        self,
        token_provider: Any,  # Callable[[], str]
        *,
        api_base: str = _GITHUB_API_BASE,
        http_timeout: int = 15,
    ) -> None:
        self._token_provider = token_provider
        self._api_base = api_base.rstrip("/")
        self._http_timeout = http_timeout

    # ---- HTTP helpers ----

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AgentCore Reference-Agent/1.0",
        }

    def _get_json(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self._http_timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            if e.code == 404:
                # 404 on a repo-scoped endpoint (/repos/owner/name/...)
                # is indistinguishable from a permissions failure — GitHub
                # deliberately collapses "no such repo" and "you can't see
                # this repo" into the same response for privacy. Surface
                # that ambiguity to the tool layer so it can explain it
                # instead of letting the model second-guess its repo pick.
                raise BackendError(
                    "GitHub returned 404. For a repo-scoped endpoint this "
                    "means the installation token can't see the repo — "
                    "either the AgentCore Reference GitHub App isn't installed on it, "
                    "the repo isn't in the App's selected-repositories "
                    "list, or the repo was deleted/renamed. This is NOT a "
                    "signal that you picked the wrong repo slug."
                ) from e
            if e.code == 403 and "rate limit" in body.lower():
                raise BackendError(
                    "GitHub search rate limit reached (30 requests/minute "
                    "per installation). Try again in a minute."
                ) from e
            raise BackendError(
                f"GitHub API error {e.code}: {body}"
            ) from e
        except urllib.error.URLError as e:
            raise BackendError(f"GitHub API network error: {e}") from e

    # ---- CodeBackend implementation ----

    def search_code(
        self, query: str, repo: str, *, max_results: int = 20
    ) -> list[SearchHit]:
        """Keyword search via GitHub Code Search API.

        Always scopes to the given repo with ``repo:owner/name``. Caller
        should still validate ``repo`` is reasonable — we don't sanitize
        it beyond URL-encoding.
        """
        if not query.strip():
            raise BackendError("Empty query")
        if not repo:
            raise BackendError("Missing repo — pass repo='owner/name'")

        per_page = min(max_results, 30)
        # Quote the free-text part so user queries with spaces work.
        q = f'{query} repo:{repo}'
        qs = urllib.parse.urlencode({"q": q, "per_page": per_page})
        url = f"{self._api_base}/search/code?{qs}"
        data = self._get_json(url)

        hits: list[SearchHit] = []
        for item in data.get("items", [])[:max_results]:
            # text_matches is only populated when Accept header requests
            # the preview mime type; we don't ask for that here to keep
            # response size small. Fall back to an empty snippet.
            text_matches = item.get("text_matches", []) or []
            snippet = ""
            if text_matches:
                fragment = text_matches[0].get("fragment", "")
                snippet = _truncate(fragment, _SNIPPET_CHAR_LIMIT)
            hits.append(
                SearchHit(
                    repo=(item.get("repository") or {}).get("full_name", repo),
                    path=item.get("path", ""),
                    html_url=item.get("html_url", ""),
                    score=float(item.get("score") or 0.0),
                    snippet=snippet,
                )
            )
        return hits

    def read_file(
        self, repo: str, path: str, *, ref: str | None = None
    ) -> FileContent:
        """Fetch a file's contents from the Contents API.

        Returns decoded UTF-8 text up to ``_FILE_SIZE_HARD_CAP_BYTES``.
        For files larger than the cap, returns the head with
        ``truncated=True``.
        """
        if not repo:
            raise BackendError("Missing repo — pass repo='owner/name'")
        if not path:
            raise BackendError("Missing path")
        if "/" not in repo:
            raise BackendError(
                f"Invalid repo {repo!r}, expected 'owner/name' format"
            )

        # Strip any leading slash the model might have added.
        path = path.lstrip("/")
        safe_path = urllib.parse.quote(path, safe="/")
        url = f"{self._api_base}/repos/{repo}/contents/{safe_path}"
        if ref:
            url += f"?{urllib.parse.urlencode({'ref': ref})}"

        data = self._get_json(url)

        if isinstance(data, list):
            raise BackendError(
                f"{path} is a directory, not a file. Pass a file path."
            )

        encoding = data.get("encoding", "")
        raw_content = data.get("content", "") or ""
        size = int(data.get("size") or 0)

        if encoding != "base64":
            raise BackendError(
                f"Unsupported encoding {encoding!r} for {path}"
            )

        try:
            decoded = base64.b64decode(raw_content)
        except Exception as e:  # noqa: BLE001
            raise BackendError(f"Failed to decode {path}: {e}") from e

        truncated = False
        if len(decoded) > _FILE_SIZE_HARD_CAP_BYTES:
            decoded = decoded[:_FILE_SIZE_HARD_CAP_BYTES]
            truncated = True

        try:
            text = decoded.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BackendError(
                f"{path} is not valid UTF-8 text (likely a binary file): {e}"
            ) from e

        return FileContent(
            repo=repo,
            path=path,
            ref=ref or data.get("sha", "")[:7] or "HEAD",
            sha=data.get("sha", ""),
            size=size,
            content=text,
            truncated=truncated,
        )

    def find_symbol(
        self, symbol: str, repo: str, *, max_results: int = 20
    ) -> list[SearchHit]:
        """Lexical "symbol" lookup via Code Search.

        Uses GitHub's ``"<symbol>" in:file`` shape which matches the
        literal token anywhere in a file's content. The first result
        is usually (but not always) the definition. Callers should
        treat this as "candidates", not "the answer".
        """
        if not symbol.strip():
            raise BackendError("Empty symbol")
        # Wrap in quotes so multi-word symbols stay together.
        return self.search_code(
            f'"{symbol}"', repo, max_results=max_results
        )

    def list_commits(
        self,
        repo: str,
        *,
        ref: str | None = None,
        path: str | None = None,
        limit: int = 10,
    ) -> list[CommitInfo]:
        """List recent commits on a ref via the Commits API.

        Hits ``GET /repos/{owner}/{repo}/commits`` which is the same
        endpoint ``git log`` would if it talked to the API directly.
        Unlike Code Search, this is NOT subject to the 30 QPM search
        limit — it uses the general 5000/hour core REST limit.

        Args:
            repo: "owner/name" slug. Required.
            ref: Optional branch, tag, or SHA. Defaults to the repo's
                 default branch (whatever ``HEAD`` points to).
            path: Optional file path to filter commits that touched
                  it — useful for "what changed in src/auth.ts recently".
            limit: Max commits to return. Capped at 100 by GitHub's
                   ``per_page``; callers asking for more get 100.
        """
        if not repo:
            raise BackendError("Missing repo — pass repo='owner/name'")
        if "/" not in repo:
            raise BackendError(
                f"Invalid repo {repo!r}, expected 'owner/name' format"
            )

        per_page = max(1, min(limit, 100))
        params: dict[str, str] = {"per_page": str(per_page)}
        if ref:
            params["sha"] = ref
        if path:
            params["path"] = path.lstrip("/")
        qs = urllib.parse.urlencode(params)
        url = f"{self._api_base}/repos/{repo}/commits?{qs}"
        data = self._get_json(url)

        if not isinstance(data, list):
            raise BackendError(
                f"Unexpected commits response shape for {repo}: {type(data).__name__}"
            )

        out: list[CommitInfo] = []
        for item in data[:limit]:
            sha = str(item.get("sha") or "")
            commit = item.get("commit") or {}
            committer = commit.get("committer") or {}
            author_info = commit.get("author") or {}
            date = str(committer.get("date") or author_info.get("date") or "")
            name = str(author_info.get("name") or "")
            email = str(author_info.get("email") or "")
            if name and email:
                author = f"{name} <{email}>"
            else:
                author = name or email or "unknown"
            raw_msg = str(commit.get("message") or "")
            message = raw_msg.split("\n", 1)[0].strip()
            html_url = str(item.get("html_url") or "")
            out.append(
                CommitInfo(
                    repo=repo,
                    sha=sha,
                    short_sha=sha[:7],
                    author=author,
                    date=date,
                    message=message,
                    html_url=html_url,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_default_backend(installation_id: str) -> CodeBackend:
    """Construct the platform-default backend for a GitHub App installation.

    Today this is always ``GithubBackend``. When ``GreptileBackend`` lands,
    this factory will check a platform flag / env var to decide — callers
    (tool functions) keep using ``build_default_backend`` and don't need
    to know which provider is active.

    The backend captures ``installation_id`` in the token provider so
    the installation token is minted on-demand per call and refreshed
    automatically by ``scm_github``'s cache.
    """
    # Late import to avoid a circular at module load time (tools.py
    # imports from this module; scm_github only needs to load once the
    # factory is called).
    from scm_github import get_installation_token

    def _token_provider() -> str:
        return get_installation_token(installation_id)

    return GithubBackend(_token_provider)


# ---------------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int) -> str:
    """Truncate with an ellipsis if over limit. Pure."""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


__all__ = [
    "BackendError",
    "CodeBackend",
    "CommitInfo",
    "FileContent",
    "GithubBackend",
    "SearchHit",
    "build_default_backend",
]
