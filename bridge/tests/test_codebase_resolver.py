"""Tests for coreAgent/app/coreAgent/codebase_resolver.py.

This file uses the same sys.path-injection trick as
``test_context_assembly.py`` — the coreAgent directory is prepended
to sys.path and the module is imported flat. The resolver module was
written with flat imports specifically so this works.

The resolver is pure (no IO, no logging, no side effects), so these
tests exercise the full public API directly without mocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Inject coreAgent onto sys.path so `import codebase_resolver` and
# `import tenant` resolve to the agent modules.
_AGENT_CODE = str(
    Path(__file__).resolve().parents[2] / "coreAgent" / "app" / "coreAgent"
)
if _AGENT_CODE not in sys.path:
    sys.path.insert(0, _AGENT_CODE)

from codebase_resolver import resolve_codebase_context  # type: ignore[import-not-found]  # noqa: E402
from tenant import CodebaseBinding, CodebasesConfig  # type: ignore[import-not-found]  # noqa: E402


# ---------------------------------------------------------------------------
# Disabled — tool-hiding switch, not ambiguity resolution
# ---------------------------------------------------------------------------

class TestDisabled:
    def test_disabled_returns_empty_block(self) -> None:
        cfg = CodebasesConfig(enabled=False)
        result = resolve_codebase_context({}, cfg)
        assert result.disabled is True
        assert result.prompt_block == ""
        assert result.bindings == []

    def test_disabled_even_with_bindings_doesnt_leak(self) -> None:
        """A disabled config must not emit any prompt text."""
        cfg = CodebasesConfig(
            enabled=False,
            default_repo="acme/platform",
            bindings=[CodebaseBinding(repo="acme/platform")],
        )
        result = resolve_codebase_context({"channel_id": "C123"}, cfg)
        assert result.disabled is True
        assert result.prompt_block == ""


# ---------------------------------------------------------------------------
# Empty bindings — the rare "App installed, no repos bound yet" case
# ---------------------------------------------------------------------------

class TestEmptyBindings:
    def test_enabled_with_no_bindings_returns_ask_block(self) -> None:
        cfg = CodebasesConfig(enabled=True)
        result = resolve_codebase_context({"channel_id": "C_ANY"}, cfg)
        assert result.disabled is False
        assert result.bindings == []
        assert "## Connected codebases" in result.prompt_block
        # Must instruct the model to ask rather than guess
        assert "ask" in result.prompt_block.lower()


# ---------------------------------------------------------------------------
# Single binding — the common "trivial case" the old SHORTLIST mis-handled
# ---------------------------------------------------------------------------

class TestSingleBinding:
    def test_single_binding_is_always_the_default(self) -> None:
        """The trivial case: one repo connected, no reason to ask."""
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[CodebaseBinding(repo="acme/backend")],
        )
        result = resolve_codebase_context({"channel_id": "C_ANY"}, cfg)
        assert result.disabled is False
        assert len(result.bindings) == 1
        # Block lists the repo and names it as the default
        assert "acme/backend" in result.prompt_block
        assert "only repo connected" in result.prompt_block.lower()

    def test_single_binding_block_does_not_gate_tool_calls(self) -> None:
        """There must be no 'DO NOT call code_* until...' language — we trust
        the model now."""
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[CodebaseBinding(repo="acme/backend")],
        )
        result = resolve_codebase_context({"channel_id": "C_NEW"}, cfg)
        lower = result.prompt_block.lower()
        assert "do not call" not in lower
        assert "before using any code" not in lower


# ---------------------------------------------------------------------------
# Multiple bindings — list them all, name a soft default, trust the model
# ---------------------------------------------------------------------------

class TestMultipleBindings:
    def test_all_bindings_appear_in_block(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="acme/platform"),
                CodebaseBinding(repo="acme/billing"),
                CodebaseBinding(repo="acme/web"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_NEW"}, cfg)
        assert "acme/platform" in result.prompt_block
        assert "acme/billing" in result.prompt_block
        assert "acme/web" in result.prompt_block

    def test_aliases_are_rendered(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(
                    repo="acme/platform",
                    aliases=["platform", "the gateway code"],
                ),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_NEW"}, cfg)
        assert "'platform'" in result.prompt_block
        assert "'the gateway code'" in result.prompt_block

    def test_default_repo_labeled_as_configured(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            default_repo="acme/web",
            bindings=[
                CodebaseBinding(repo="acme/platform"),
                CodebaseBinding(repo="acme/billing"),
                CodebaseBinding(repo="acme/web"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_NEW"}, cfg)
        # The default line should name acme/web and mention "install-time"
        assert "acme/web" in result.prompt_block
        lower = result.prompt_block.lower()
        assert "install-time" in lower or "configured" in lower

    def test_no_default_falls_back_to_first_binding_weakly(self) -> None:
        """With no operator / memory / default_repo match, the first binding
        is a weak tiebreaker only."""
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="acme/platform"),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_NEW"}, cfg)
        lower = result.prompt_block.lower()
        assert "weak" in lower or "tiebreaker" in lower


# ---------------------------------------------------------------------------
# Explicit operator channel binding — still authoritative but softer wording
# ---------------------------------------------------------------------------

class TestOperatorChannelBinding:
    def test_operator_channel_binding_is_labeled(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="acme/platform", channels=["C_ONCALL"]),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_ONCALL"}, cfg)
        lower = result.prompt_block.lower()
        # Default line mentions operator / tenant-set framing
        assert "acme/platform" in result.prompt_block
        assert "operator" in lower or "set by the tenant" in lower

    def test_operator_binding_beats_default_repo(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            default_repo="acme/billing",
            bindings=[
                CodebaseBinding(repo="acme/platform", channels=["C_ONCALL"]),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_ONCALL"}, cfg)
        # The default callout should be acme/platform, not acme/billing,
        # because operator channel binding has highest precedence
        # (first to appear in a default-labeled context, anyway)
        default_section = result.prompt_block.lower().split("**default", 1)[1]
        assert "acme/platform" in default_section

    def test_operator_binding_without_match_falls_through(self) -> None:
        """A bindings.channels list that doesn't contain the current channel
        must NOT be treated as a match — that was the original bug."""
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="acme/platform", channels=["C_FOO"]),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C_BAR"}, cfg)
        # We still emit a block listing both repos (no more SHORTLIST gate),
        # but the default line should NOT claim the tenant operator set it
        lower = result.prompt_block.lower()
        assert "set by the tenant operator" not in lower


# ---------------------------------------------------------------------------
# Semantic hint — informative, not authoritative
# ---------------------------------------------------------------------------

class TestSemanticHint:
    def test_known_hint_labels_default_as_remembered(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            default_repo="acme/platform",
            bindings=[
                CodebaseBinding(repo="acme/platform"),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context(
            {"channel_id": "C_NEW"}, cfg, semantic_hint="acme/billing"
        )
        lower = result.prompt_block.lower()
        # Default line should mention memory / "most recently used"
        assert "acme/billing" in result.prompt_block
        assert "remember" in lower or "recently used" in lower or "memory" in lower

    def test_unknown_hint_is_dropped_silently(self) -> None:
        """A hint naming a repo NOT in bindings must not be surfaced — we
        don't want to teach the model about repos it can't access."""
        cfg = CodebasesConfig(
            enabled=True,
            default_repo="acme/platform",
            bindings=[
                CodebaseBinding(repo="acme/platform"),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context(
            {"channel_id": "C_NEW"}, cfg, semantic_hint="evil/unknown"
        )
        assert "evil/unknown" not in result.prompt_block
        # Default falls back to the configured default_repo
        lower = result.prompt_block.lower()
        default_section = lower.split("**default", 1)[1]
        assert "acme/platform" in default_section

    def test_operator_binding_wins_over_semantic_hint(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="acme/platform", channels=["C_ONCALL"]),
                CodebaseBinding(repo="acme/billing"),
            ],
        )
        result = resolve_codebase_context(
            {"channel_id": "C_ONCALL"},
            cfg,
            semantic_hint="acme/billing",
        )
        # Operator binding is higher precedence
        lower = result.prompt_block.lower()
        default_section = lower.split("**default", 1)[1]
        assert "acme/platform" in default_section
        assert "operator" in default_section

    def test_empty_hint_treated_as_no_hint(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[CodebaseBinding(repo="a/b")],
        )
        a = resolve_codebase_context({"channel_id": "C"}, cfg, semantic_hint="")
        b = resolve_codebase_context({"channel_id": "C"}, cfg)
        assert a.prompt_block == b.prompt_block

    def test_none_hint_equivalent_to_no_hint(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[CodebaseBinding(repo="a/b")],
        )
        a = resolve_codebase_context({"channel_id": "C"}, cfg, semantic_hint=None)
        b = resolve_codebase_context({"channel_id": "C"}, cfg)
        assert a.prompt_block == b.prompt_block


# ---------------------------------------------------------------------------
# Anti-regression: no channel-hardline language, no tool-gating language
# ---------------------------------------------------------------------------

class TestNoChannelHardlineLanguage:
    """The user's explicit complaint: the old resolver treated
    channel == repository as a hard rule, which is wrong because a
    channel can reference many repos over its lifetime. None of the
    blocks we emit should contain the offending phrases."""

    _CONFIGS = [
        CodebasesConfig(enabled=True, bindings=[CodebaseBinding(repo="a/b")]),
        CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="a/b"),
                CodebaseBinding(repo="c/d"),
            ],
        ),
        CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="a/b", channels=["C_ONCALL"]),
                CodebaseBinding(repo="c/d"),
            ],
        ),
    ]
    _CHANNELS = ["C_NEW", "C_ONCALL", ""]

    def _blocks(self) -> list[str]:
        out: list[str] = []
        for cfg in self._CONFIGS:
            for ch in self._CHANNELS:
                r = resolve_codebase_context({"channel_id": ch}, cfg)
                out.append(r.prompt_block)
        return out

    def test_no_going_forward_language(self) -> None:
        for block in self._blocks():
            assert "going forward" not in block.lower()

    def test_no_primary_for_this_channel_language(self) -> None:
        for block in self._blocks():
            lower = block.lower()
            assert "for this channel" not in lower
            assert "in this channel" not in lower

    def test_no_tool_gating_language(self) -> None:
        """Previous SHORTLIST blocks explicitly forbade code_* tool
        calls until the user picked. That's gone."""
        for block in self._blocks():
            lower = block.lower()
            assert "do not call" not in lower
            assert "before using any code" not in lower

    def test_no_stop_on_topic_shift_language(self) -> None:
        for block in self._blocks():
            assert "STOP" not in block

    def test_no_acknowledgment_template_coaching(self) -> None:
        """The old block coached the model to say literally 'I'll use
        X going forward' so the SEMANTIC strategy could extract it."""
        for block in self._blocks():
            lower = block.lower()
            assert "acknowledge" not in lower
            assert "i'll use" not in lower


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------

class TestStructuralInvariants:
    def test_block_always_starts_with_header_when_not_disabled(self) -> None:
        cfgs = [
            CodebasesConfig(enabled=True),  # empty bindings
            CodebasesConfig(
                enabled=True,
                bindings=[CodebaseBinding(repo="a/b")],
            ),
            CodebasesConfig(
                enabled=True,
                bindings=[
                    CodebaseBinding(repo="a/b"),
                    CodebaseBinding(repo="c/d"),
                ],
            ),
        ]
        for cfg in cfgs:
            result = resolve_codebase_context({"channel_id": "C"}, cfg)
            assert result.disabled is False
            assert result.prompt_block.startswith("## Connected codebases")

    def test_missing_channel_id_does_not_crash(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[CodebaseBinding(repo="a/b", channels=["C_ONCALL"])],
        )
        result = resolve_codebase_context({}, cfg)
        assert result.disabled is False
        assert "a/b" in result.prompt_block

    def test_none_channel_id_does_not_crash(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[CodebaseBinding(repo="a/b", channels=["C_ONCALL"])],
        )
        result = resolve_codebase_context({"channel_id": None}, cfg)
        assert result.disabled is False
        assert "a/b" in result.prompt_block

    def test_bindings_field_carries_full_list(self) -> None:
        cfg = CodebasesConfig(
            enabled=True,
            bindings=[
                CodebaseBinding(repo="a/b"),
                CodebaseBinding(repo="c/d"),
                CodebaseBinding(repo="e/f"),
            ],
        )
        result = resolve_codebase_context({"channel_id": "C"}, cfg)
        assert [b.repo for b in result.bindings] == ["a/b", "c/d", "e/f"]
