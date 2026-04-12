"""Tests for coreAgent/app/coreAgent/code_backend.py.

Uses the same sys.path-injection pattern as test_codebase_resolver.py —
code_backend.py uses flat imports so it imports cleanly from the
bridge venv without needing the full agent runtime deps.

The tests mock ``urllib.request.urlopen`` so no network traffic happens.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

# Inject coreAgent onto sys.path so `import code_backend` resolves.
_AGENT_CODE = str(
    Path(__file__).resolve().parents[2] / "coreAgent" / "app" / "coreAgent"
)
if _AGENT_CODE not in sys.path:
    sys.path.insert(0, _AGENT_CODE)

from code_backend import (  # type: ignore[import-not-found]
    BackendError,
    CommitInfo,
    FileContent,
    GithubBackend,
    SearchHit,
    _truncate,
    build_default_backend,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello", 20) == "hello"

    def test_exact_length_unchanged(self) -> None:
        assert _truncate("hello", 5) == "hello"

    def test_long_text_truncated(self) -> None:
        assert _truncate("hello world", 7) == "hell..."

    def test_truncation_leaves_room_for_ellipsis(self) -> None:
        """A 10-char input with limit 6 should fit 3 chars + '...'."""
        assert _truncate("abcdefghij", 6) == "abc..."


# ---------------------------------------------------------------------------
# Dataclass round-trips — basic sanity
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_search_hit_defaults(self) -> None:
        hit = SearchHit(repo="a/b", path="src/foo.py", html_url="https://x")
        assert hit.score == 0.0
        assert hit.snippet == ""

    def test_file_content_truncated_default_false(self) -> None:
        f = FileContent(
            repo="a/b", path="x", ref="main", sha="abc", size=10, content="x"
        )
        assert f.truncated is False


# ---------------------------------------------------------------------------
# urllib mocking helper
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Mimic the object yielded by urllib.request.urlopen as a context manager."""

    def __init__(self, payload: dict[str, Any] | list[Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _UrlCapture:
    """Captures the last URL + headers passed to urlopen for assertions."""

    def __init__(self, response: Any) -> None:
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self._response = response

    def __call__(self, req: Any, timeout: int | None = None) -> Any:
        self.last_url = req.full_url
        self.last_headers = dict(req.headers)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def backend() -> GithubBackend:
    return GithubBackend(lambda: "ghs_fake_token")


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, capture: _UrlCapture) -> None:
    monkeypatch.setattr("urllib.request.urlopen", capture)


# ---------------------------------------------------------------------------
# search_code
# ---------------------------------------------------------------------------

class TestSearchCode:
    def test_happy_path_builds_correct_url_and_parses_hits(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "total_count": 2,
            "incomplete_results": False,
            "items": [
                {
                    "path": "src/auth/login.ts",
                    "html_url": "https://github.com/acme/platform/blob/main/src/auth/login.ts",
                    "score": 3.14,
                    "repository": {"full_name": "acme/platform"},
                    "text_matches": [
                        {"fragment": "function authenticateUser(token: string) {"}
                    ],
                },
                {
                    "path": "src/auth/logout.ts",
                    "html_url": "https://github.com/acme/platform/blob/main/src/auth/logout.ts",
                    "score": 1.5,
                    "repository": {"full_name": "acme/platform"},
                },
            ],
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)

        hits = backend.search_code("authenticate", "acme/platform", max_results=10)

        assert len(hits) == 2
        assert hits[0].path == "src/auth/login.ts"
        assert hits[0].repo == "acme/platform"
        assert hits[0].score == 3.14
        assert "authenticateUser" in hits[0].snippet
        # Second hit has no text_matches → empty snippet
        assert hits[1].snippet == ""

        # URL construction
        assert cap.last_url is not None
        assert "/search/code" in cap.last_url
        assert "repo%3Aacme%2Fplatform" in cap.last_url  # URL-encoded
        assert "per_page=10" in cap.last_url

    def test_auth_header_uses_token_provider(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = _UrlCapture(_FakeResponse({"items": []}))
        _patch_urlopen(monkeypatch, cap)
        backend.search_code("x", "a/b")
        assert cap.last_headers is not None
        # urllib.request.Request normalizes header keys via str.capitalize(),
        # so "X-GitHub-Api-Version" becomes "X-github-api-version". Compare
        # case-insensitively to avoid the implementation detail.
        lowered = {k.lower(): v for k, v in cap.last_headers.items()}
        assert lowered["authorization"] == "Bearer ghs_fake_token"
        assert lowered["x-github-api-version"] == "2022-11-28"
        assert lowered["accept"] == "application/vnd.github+json"

    def test_max_results_respected(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # GitHub returns 30; we ask for 5; we should trim client-side.
        items = [
            {
                "path": f"file{i}.py",
                "html_url": "https://x",
                "repository": {"full_name": "a/b"},
                "score": 1.0,
            }
            for i in range(30)
        ]
        cap = _UrlCapture(_FakeResponse({"items": items}))
        _patch_urlopen(monkeypatch, cap)
        hits = backend.search_code("x", "a/b", max_results=5)
        assert len(hits) == 5

    def test_empty_query_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="Empty query"):
            backend.search_code("   ", "a/b")

    def test_missing_repo_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="Missing repo"):
            backend.search_code("x", "")

    def test_snippet_truncation(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        huge_fragment = "A" * 2000
        payload = {
            "items": [
                {
                    "path": "x.py",
                    "html_url": "https://x",
                    "repository": {"full_name": "a/b"},
                    "text_matches": [{"fragment": huge_fragment}],
                }
            ]
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        hits = backend.search_code("x", "a/b")
        # _SNIPPET_CHAR_LIMIT = 400
        assert len(hits[0].snippet) == 400
        assert hits[0].snippet.endswith("...")

    def test_rate_limit_wrapped_in_backend_error(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b'{"message": "API rate limit exceeded"}'
        err = urllib.error.HTTPError(
            "https://api.github.com/search/code",
            403,
            "Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )
        cap = _UrlCapture(err)
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="rate limit"):
            backend.search_code("x", "a/b")

    def test_404_wrapped_in_backend_error(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = urllib.error.HTTPError(
            "https://api.github.com/search/code",
            404,
            "Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"message":"not found"}'),
        )
        cap = _UrlCapture(err)
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="GitHub returned 404"):
            backend.search_code("x", "a/b")

    def test_generic_http_error_wrapped(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = urllib.error.HTTPError(
            "https://api.github.com/search/code",
            502,
            "Bad Gateway",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"message":"upstream"}'),
        )
        cap = _UrlCapture(err)
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="502"):
            backend.search_code("x", "a/b")

    def test_network_error_wrapped(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = urllib.error.URLError("connection refused")
        cap = _UrlCapture(err)
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="network error"):
            backend.search_code("x", "a/b")


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_happy_path_decodes_base64(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = b"hello world\nsecond line\n"
        payload = {
            "name": "readme.txt",
            "path": "readme.txt",
            "sha": "abc123def",
            "size": len(body),
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(body).decode("ascii"),
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)

        result = backend.read_file("acme/platform", "readme.txt")

        assert result.content == "hello world\nsecond line\n"
        assert result.path == "readme.txt"
        assert result.repo == "acme/platform"
        assert result.sha == "abc123def"
        assert result.size == len(body)
        assert result.truncated is False

    def test_uses_custom_ref(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "sha": "xxx",
            "size": 0,
            "encoding": "base64",
            "content": "",
            "type": "file",
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        result = backend.read_file("a/b", "file.py", ref="develop")
        assert cap.last_url is not None
        assert "ref=develop" in cap.last_url
        assert result.ref == "develop"

    def test_strips_leading_slash_from_path(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "sha": "xxx", "size": 0, "encoding": "base64", "content": "", "type": "file",
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        backend.read_file("a/b", "/src/foo.py")
        assert cap.last_url is not None
        # URL should contain "contents/src/foo.py" not "contents//src/foo.py"
        assert "contents/src/foo.py" in cap.last_url

    def test_url_encodes_path_with_spaces(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "sha": "xxx", "size": 0, "encoding": "base64", "content": "", "type": "file",
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        backend.read_file("a/b", "docs/user guide.md")
        assert cap.last_url is not None
        assert "user%20guide.md" in cap.last_url

    def test_directory_returns_error(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Contents API returns a LIST when path is a directory.
        cap = _UrlCapture(_FakeResponse([{"name": "a"}, {"name": "b"}]))
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="directory"):
            backend.read_file("a/b", "src")

    def test_binary_file_raises(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Non-UTF-8 bytes (e.g. a PNG header)
        binary = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        payload = {
            "sha": "x",
            "size": len(binary),
            "encoding": "base64",
            "content": base64.b64encode(binary).decode("ascii"),
            "type": "file",
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="binary"):
            backend.read_file("a/b", "image.png")

    def test_truncation_for_huge_files(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 100 KB of 'x' — exceeds the 64 KB cap
        huge = "x" * (100 * 1024)
        payload = {
            "sha": "x",
            "size": len(huge.encode()),
            "encoding": "base64",
            "content": base64.b64encode(huge.encode()).decode("ascii"),
            "type": "file",
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        result = backend.read_file("a/b", "big.txt")
        assert result.truncated is True
        assert len(result.content) == 64 * 1024

    def test_unsupported_encoding_raises(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {
            "sha": "x",
            "size": 0,
            "encoding": "none",
            "content": "",
            "type": "file",
        }
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)
        with pytest.raises(BackendError, match="encoding"):
            backend.read_file("a/b", "x")

    def test_invalid_repo_format_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="owner/name"):
            backend.read_file("no-slash", "x")

    def test_missing_path_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="path"):
            backend.read_file("a/b", "")


# ---------------------------------------------------------------------------
# find_symbol
# ---------------------------------------------------------------------------

class TestFindSymbol:
    def test_delegates_to_search_with_quoted_query(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = {"items": [{"path": "a.py", "html_url": "https://x", "repository": {"full_name": "a/b"}}]}
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)

        hits = backend.find_symbol("authenticateUser", "a/b")
        assert len(hits) == 1
        # Quoted symbol is part of the URL query
        assert cap.last_url is not None
        assert "%22authenticateUser%22" in cap.last_url

    def test_empty_symbol_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="Empty"):
            backend.find_symbol("   ", "a/b")


# ---------------------------------------------------------------------------
# list_commits
# ---------------------------------------------------------------------------

def _commit_payload(
    sha: str,
    message: str,
    author_name: str = "Ada Lovelace",
    author_email: str = "ada@example.com",
    date: str = "2026-04-11T12:34:56Z",
) -> dict[str, Any]:
    """Build one item in the GET /commits response shape."""
    return {
        "sha": sha,
        "html_url": f"https://github.com/acme/platform/commit/{sha}",
        "commit": {
            "message": message,
            "author": {
                "name": author_name,
                "email": author_email,
                "date": date,
            },
            "committer": {
                "name": author_name,
                "email": author_email,
                "date": date,
            },
        },
    }


class TestListCommits:
    def test_happy_path_parses_commits(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = [
            _commit_payload(
                "abc1234def5678901234567890123456789012ab",
                "fix: handle null token\n\nFull body with explanation.",
            ),
            _commit_payload(
                "def9876543210987654321098765432109876543",
                "feat: add rate limiter",
            ),
        ]
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)

        commits = backend.list_commits("acme/platform", limit=10)

        assert len(commits) == 2
        first = commits[0]
        assert isinstance(first, CommitInfo)
        assert first.repo == "acme/platform"
        assert first.sha == "abc1234def5678901234567890123456789012ab"
        assert first.short_sha == "abc1234"
        # Only the subject line, not the full body
        assert first.message == "fix: handle null token"
        assert first.author == "Ada Lovelace <ada@example.com>"
        assert first.date == "2026-04-11T12:34:56Z"
        assert "/commit/abc1234" in first.html_url

        assert cap.last_url is not None
        assert "/repos/acme/platform/commits" in cap.last_url
        assert "per_page=10" in cap.last_url

    def test_ref_filter_passes_sha_param(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = _UrlCapture(_FakeResponse([]))
        _patch_urlopen(monkeypatch, cap)

        backend.list_commits("acme/platform", ref="develop", limit=5)

        assert cap.last_url is not None
        assert "sha=develop" in cap.last_url
        assert "per_page=5" in cap.last_url

    def test_path_filter_passes_path_param_stripped(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = _UrlCapture(_FakeResponse([]))
        _patch_urlopen(monkeypatch, cap)

        backend.list_commits("acme/platform", path="/src/auth.ts")

        assert cap.last_url is not None
        # Leading slash must be stripped before urlencoding
        assert "path=src" in cap.last_url and "auth.ts" in cap.last_url

    def test_empty_result_returns_empty_list(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = _UrlCapture(_FakeResponse([]))
        _patch_urlopen(monkeypatch, cap)
        commits = backend.list_commits("acme/platform")
        assert commits == []

    def test_limit_caps_at_100(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cap = _UrlCapture(_FakeResponse([]))
        _patch_urlopen(monkeypatch, cap)

        backend.list_commits("acme/platform", limit=5000)

        assert cap.last_url is not None
        assert "per_page=100" in cap.last_url

    def test_missing_repo_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="Missing repo"):
            backend.list_commits("")

    def test_invalid_repo_format_raises(self, backend: GithubBackend) -> None:
        with pytest.raises(BackendError, match="Invalid repo"):
            backend.list_commits("not-a-slug")

    def test_404_raises_backend_error(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = urllib.error.HTTPError(
            "https://api.github.com/repos/acme/missing/commits",
            404,
            "Not Found",
            {},  # type: ignore[arg-type]
            io.BytesIO(b'{"message":"Not Found"}'),
        )
        cap = _UrlCapture(err)
        _patch_urlopen(monkeypatch, cap)

        with pytest.raises(BackendError, match="GitHub returned 404"):
            backend.list_commits("acme/missing")

    def test_missing_committer_date_falls_back_to_author_date(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = [
            {
                "sha": "abc" * 14,
                "html_url": "https://x",
                "commit": {
                    "message": "chore: bump deps",
                    "author": {
                        "name": "Grace Hopper",
                        "email": "grace@example.com",
                        "date": "2026-03-01T00:00:00Z",
                    },
                    # no committer field — use author.date
                },
            }
        ]
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)

        commits = backend.list_commits("acme/platform")
        assert commits[0].date == "2026-03-01T00:00:00Z"

    def test_missing_author_fields_handled(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = [
            {
                "sha": "a" * 40,
                "html_url": "https://x",
                "commit": {
                    "message": "unknown author",
                    "author": {},
                    "committer": {"date": "2026-01-01T00:00:00Z"},
                },
            }
        ]
        cap = _UrlCapture(_FakeResponse(payload))
        _patch_urlopen(monkeypatch, cap)

        commits = backend.list_commits("acme/platform")
        assert commits[0].author == "unknown"

    def test_non_list_response_raises(
        self, backend: GithubBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The GitHub commits API returns a JSON array at top-level. If we
        # get a dict instead, that's either an error payload or a schema
        # surprise — fail loud, don't silently return empty.
        cap = _UrlCapture(_FakeResponse({"message": "something weird"}))
        _patch_urlopen(monkeypatch, cap)

        with pytest.raises(BackendError, match="Unexpected commits response"):
            backend.list_commits("acme/platform")


# ---------------------------------------------------------------------------
# build_default_backend factory
# ---------------------------------------------------------------------------

class TestBuildDefaultBackend:
    def test_returns_github_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stub scm_github.get_installation_token — we don't want a real HTTP call.
        import scm_github  # type: ignore[import-not-found]

        monkeypatch.setattr(scm_github, "get_installation_token", lambda _: "ghs_stub")

        backend = build_default_backend("12345")
        assert isinstance(backend, GithubBackend)
        # The backend's token provider should pull through to our stub
        assert backend._token_provider() == "ghs_stub"
