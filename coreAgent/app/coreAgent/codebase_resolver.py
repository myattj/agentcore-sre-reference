"""Per-tenant codebase context injector for the pre-LLM pipeline.

Pure function — given a ``CodebasesConfig`` and the request ``ctx``,
return a ``CodebaseContext`` carrying a single prompt block that lists
the tenant's connected repos and gives the model enough information
to pick the right one on its own. Called from
``context_assembler.assemble_context``.

## Design: trust the LLM, inject facts

Earlier versions of this module were a four-state machine
(CONFIRMED / SHORTLIST / UNKNOWN / DISABLED) that tried to puppeteer
the model into asking before searching, stopping on topic shifts, and
saying specific acknowledgment phrases so the memory layer could
extract them. That was doing intent-detection work the model is
already better at — and it introduced "for this channel" rigidity the
user flagged: channels can reasonably reference many repos, and a
one-shot "which codebase?" prompt when there's only one connected
repo is just noise.

The new shape is flat:

  - ``disabled=True`` when ``codebases.enabled=False`` — no prompt
    injection, and ``main.py`` hides the ``code_*`` tools from the
    effective tool list.
  - Otherwise, one prompt block lists every connected repo with its
    default branch and any aliases, names a soft default (operator
    channel binding > memory hint > ``default_repo`` > first binding)
    WITH its reason, and tells the model to match the user's intent
    — switch to another connected repo when the user names one, and
    only call ``ask_codebase_choice`` when the message is genuinely
    ambiguous.

The resolver does **not** read the user message. The LLM reads the
message; the resolver just gives it the facts. This avoids the
silent-wrong failure mode where a keyword-based rule picks the wrong
repo and then confidently runs a tool on it.

## Soft-default precedence (for display / "I'd default to..." wording)

  1. An explicit ``codebases.bindings[i].channels`` entry that
     contains the current ``ctx["channel_id"]``. Labeled as an
     operator-set default — authoritative but still overridable by
     the model if the user clearly means another connected repo.
  2. ``semantic_hint`` when it matches a known binding. Labeled as
     "most recently used here" — informative, not authoritative.
  3. ``codebases.default_repo`` when it matches a known binding.
     Labeled as the install-time default.
  4. The first binding in the list, unlabeled.

The soft default is only surfaced in the prompt wording. Tool calls
still require the model to pass ``repo='owner/name'`` explicitly —
the ctx no longer carries a ``primary_repo`` fallback that could
silently pick a repo the model didn't choose.

## Why a separate module from context_assembler.py

Historical: the assembler used relative imports that broke the
bridge's sys.path-based test harness, so the resolver was split out
with flat imports so its pure functions stay unit-testable. The
assembler has since moved to flat imports too, but keeping this
module separate is still cleaner — it's the one place where the
binding-presentation rules live.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tenant import CodebaseBinding, CodebasesConfig


@dataclass
class CodebaseContext:
    """Result of ``resolve_codebase_context``.

    ``disabled`` is True when the tenant has ``codebases.enabled=False``.
    Callers should skip the prompt injection entirely in that case;
    ``main.py`` also drops the ``code_*`` catalog tools from the
    effective tool list.

    ``bindings`` is the tenant's full binding list, in declaration
    order. Empty when the tenant has enabled codebases but hasn't
    bound any repos yet (App installed, warm-start not run, etc.).

    ``prompt_block`` is ready to append to the effective system
    prompt as-is. Empty string when ``disabled=True``.
    """
    disabled: bool = False
    bindings: list[CodebaseBinding] = field(default_factory=list)
    prompt_block: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_codebase_context(
    ctx: dict[str, Any],
    codebases: CodebasesConfig,
    *,
    semantic_hint: str | None = None,
) -> CodebaseContext:
    """Build a codebase-context block for the effective system prompt.

    Returns a ``CodebaseContext`` carrying a single prompt block (or
    an empty block when disabled / an ask-the-user block when the
    tenant has no bindings yet). Never raises.

    ``semantic_hint`` is an optional repo slug from AgentCore Memory's
    SEMANTIC namespace — when it matches a binding, the block labels
    that binding as "most recently used here" so the model can lean
    on it as a soft default. A hint that doesn't match any binding is
    silently dropped (don't teach the model about a repo it can't
    access). The caller (the context assembler) is responsible for
    its own bounded-answer filter on the memory retrieval side.
    """
    if not codebases.enabled:
        return CodebaseContext(disabled=True)

    bindings = list(codebases.bindings)

    if not bindings:
        return CodebaseContext(
            disabled=False,
            bindings=[],
            prompt_block=_empty_bindings_block(),
        )

    channel_id = (ctx.get("channel_id") or "").strip()
    default, reason = _pick_soft_default(
        bindings, codebases.default_repo, channel_id, semantic_hint
    )
    return CodebaseContext(
        disabled=False,
        bindings=bindings,
        prompt_block=_bindings_block(bindings, default, reason),
    )


# ---------------------------------------------------------------------------
# Soft-default selection (pure)
# ---------------------------------------------------------------------------

def _pick_soft_default(
    bindings: list[CodebaseBinding],
    default_repo: str | None,
    channel_id: str,
    semantic_hint: str | None,
) -> tuple[CodebaseBinding, str]:
    """Pick the binding the prompt should name as the soft default.

    Returns ``(binding, reason)``. The reason is one of:

      - ``"operator"``   — explicit ``bindings[i].channels`` match
      - ``"remembered"`` — semantic hint matching a binding
      - ``"configured"`` — ``codebases.default_repo`` matching a binding
      - ``"sole"``       — exactly one binding (trivial case)
      - ``"first"``      — fallback to the first binding in the list

    Never raises; ``bindings`` must be non-empty (caller enforces).
    """
    # Priority 1: operator-set channel binding.
    if channel_id:
        for b in bindings:
            if channel_id in b.channels:
                return b, "operator"

    # Priority 2: semantic hint from memory.
    if semantic_hint:
        for b in bindings:
            if b.repo == semantic_hint:
                return b, "remembered"

    # Priority 3: ``default_repo`` from install-time config.
    if default_repo:
        for b in bindings:
            if b.repo == default_repo:
                return b, "configured"

    # Priority 4: the only binding, or the first one.
    if len(bindings) == 1:
        return bindings[0], "sole"
    return bindings[0], "first"


# ---------------------------------------------------------------------------
# Prompt block builders (pure, no side effects)
# ---------------------------------------------------------------------------

_HEADER = "## Connected codebases"


def _bindings_block(
    bindings: list[CodebaseBinding],
    default: CodebaseBinding,
    reason: str,
) -> str:
    """Build the single prompt block that lists all connected repos.

    The block carries:

      - a bullet list of every binding (repo, branch, aliases)
      - a one-line soft-default callout naming the default and why
      - instructions to match user intent and switch when the user
        names another connected repo
      - a suggestion to call ``ask_codebase_choice`` when genuinely
        ambiguous, framed as a UX nicety not a hard gate
    """
    listing = "\n".join(_render_binding_line(b) for b in bindings)
    default_line = _render_default_line(default, reason)

    guidance = (
        "**Picking the right one:** reason about it. Read the user's "
        "message, look at the list above, pick the repo that fits. "
        "If they name a repo (full slug, short name, or alias), use "
        "that. If the message clearly points at a different connected "
        "repo than the default, switch. Trust your read of the "
        "situation — don't second-guess a pick you've already made "
        "just because a later tool call returns an error. Backend "
        "errors (404s, rate limits, auth failures) are about access, "
        "not about whether you chose the right name.\n\n"
        "**Don't announce repo defaults.** Never narrate your "
        "selection process or tell the user which repo you picked "
        "as a default. Just use the repo silently — no "
        "meta-commentary needed.\n\n"
        "**Calling code tools:** pass ``repo='owner/name'`` explicitly "
        "on every call — pick the slug from the list above. There is "
        "no silent default; an omitted repo is an error.\n\n"
        "**Need more signal before picking?** Call "
        "``inspect_codebase_context`` — it returns extra hints (channel "
        "name + topic, user profile, channel-pinned bindings, recent "
        "memory hint) you can reason from. It makes no decision; it "
        "just surfaces facts. Use it when the message + thread alone "
        "don't give you enough to choose confidently.\n\n"
        "**If you still can't tell which repo is meant** after "
        "reading the message, the list, and any inspection you've "
        "done, asking the user is fine — do it in prose, or call "
        "``ask_codebase_choice`` for a one-click UI when there are "
        "only a few plausible candidates. Either is fine; pick "
        "whichever reads more naturally in context. This is a UX "
        "choice, not a fallback procedure."
    )

    return f"{_HEADER}\n{listing}\n\n{default_line}\n\n{guidance}"


def _render_binding_line(b: CodebaseBinding) -> str:
    """One bullet line per binding."""
    parts = [f"- `{b.repo}` (branch: `{b.default_branch}`)"]
    if b.aliases:
        alias_text = ", ".join(repr(a) for a in b.aliases)
        parts.append(f" — aliases: {alias_text}")
    return "".join(parts)


def _render_default_line(default: CodebaseBinding, reason: str) -> str:
    """One-line callout for the soft default + why."""
    repo = f"`{default.repo}`"
    if reason == "sole":
        return (
            f"**Default:** {repo} — the only repo connected for this "
            f"tenant. Use it unless the user names another (they can't "
            f"right now, but an explicit mention should still be honored)."
        )
    if reason == "operator":
        return (
            f"**Default:** {repo} — set by the tenant operator as the "
            f"primary for this space. Lean on it, but honor the user's "
            f"intent if they clearly mean another connected repo."
        )
    if reason == "remembered":
        return (
            f"**Default:** {repo} — most recently used in this scope "
            f"(from memory of earlier conversations). Informative, not "
            f"authoritative — switch if the current message points "
            f"somewhere else."
        )
    if reason == "configured":
        return (
            f"**Default:** {repo} — install-time default for this "
            f"tenant. Use it when the user's message doesn't point "
            f"anywhere specific."
        )
    # reason == "first"
    return (
        f"**Default (weak):** {repo} — no operator binding, memory "
        f"hint, or configured default matched, so this is just the "
        f"first entry in the list. Treat it as a tiebreaker only; "
        f"prefer whatever the user's message actually points to."
    )


def _empty_bindings_block() -> str:
    """Block for the rare ``enabled=True`` with zero bindings case."""
    return (
        f"{_HEADER}\n"
        "The GitHub App is installed for this tenant but no "
        "repositories have been bound yet. If the user asks a code "
        "question, ask them for the `owner/name` slug — don't guess "
        "from context. Once they give one, pass it as "
        "``repo='owner/name'`` to any `code_*` tool."
    )


__all__ = [
    "CodebaseContext",
    "resolve_codebase_context",
]
