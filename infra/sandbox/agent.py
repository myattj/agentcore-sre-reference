"""PR sandbox agent — Claude tool-use loop for real code changes.

The agent reads the task description, explores the cloned repo, makes
code changes, and returns a structured result for the entrypoint to
commit, push, and turn into a pull request.

The agent runs a standard Anthropic API tool-use loop:
  1. System prompt built from task_description + context_hint
  2. Six tools: read_file, write_file, edit_file, list_directory,
     run_command, submit_changes
  3. Token budget enforcement with soft (85%) and hard (95%) caps
  4. Prompt caching on the system block for cost efficiency

The entrypoint calls two public functions:
  - ``run_agent_loop()`` — the agentic editing phase
  - ``generate_pr_metadata()`` — a single-call PR title/body/commit
    message generator from the git diff

Security: this module runs inside the sandbox container (unprivileged
``sandbox`` user, no tenant secrets, egress-only networking). All file
operations are scoped to the work_dir (the cloned repo). Commands run
via subprocess with timeouts.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import anthropic

log = logging.getLogger("sandbox.agent")

# Canonical Anthropic API model ID. Anthropic's Claude 4.6 aliases are
# dateless; date-suffixed IDs for these models do not exist.
DEFAULT_MODEL = "claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Pricing (per million tokens, USD)
# ---------------------------------------------------------------------------

PRICING: dict[str, dict[str, float]] = {
    DEFAULT_MODEL: {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
}

# Fallback for unknown models — use Sonnet pricing as a conservative default.
_DEFAULT_PRICING = PRICING[DEFAULT_MODEL]

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TokenBudget:
    """Tracks cumulative token usage and dollar cost for the agent loop."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    budget_dollars: float = 5.0
    model: str = DEFAULT_MODEL

    @property
    def cost_dollars(self) -> float:
        rates = PRICING.get(self.model, _DEFAULT_PRICING)
        return (
            self.input_tokens * rates["input"]
            + self.output_tokens * rates["output"]
            + self.cache_creation_input_tokens * rates["cache_write"]
            + self.cache_read_input_tokens * rates["cache_read"]
        ) / 1_000_000

    @property
    def cost_cents(self) -> int:
        return int(self.cost_dollars * 100)

    @property
    def fraction_used(self) -> float:
        if self.budget_dollars <= 0:
            return 1.0
        return self.cost_dollars / self.budget_dollars

    def update(self, usage: anthropic.types.Usage) -> None:
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0


@dataclass
class AgentResult:
    """Returned by ``run_agent_loop`` to the entrypoint."""

    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    token_budget: TokenBudget = field(default_factory=TokenBudget)
    error: str = ""


@dataclass
class PRMetadata:
    """Returned by ``generate_pr_metadata``."""

    title: str = ""
    body: str = ""
    commit_message: str = ""


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic API schema)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repository. Returns content with line "
            "numbers. Use offset/limit for large files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the repository root.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Line number to start from (1-based). Default: 1.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max lines to return. Default: 2000.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or overwrite a file. Creates parent directories if "
            "needed. Use for new files or complete rewrites."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the repository root.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Make a targeted edit to a file by replacing a specific string. "
            "The old_string must appear exactly ONCE in the file — include "
            "enough surrounding context to be unique. Prefer this over "
            "write_file for modifying existing files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the repository root.",
                },
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find (must match once).",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_directory",
        "description": (
            "List files and directories. Use to understand project "
            "structure before reading or editing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from the repository root. Default: '.' (root).",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List recursively. Default: false.",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Max recursion depth (only when recursive=true). Default: 3.",
                },
            },
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the repository directory. Use for "
            "grep, test execution, linting, or any CLI operation. "
            "Commands run with a timeout (default 60s, max 120s)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds. Default: 60, max: 120.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "submit_changes",
        "description": (
            "Call this when you have finished making all changes. Provide "
            "a summary of what you changed and why, plus the list of files "
            "you modified. This ends the editing session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "A concise summary of all changes made and the "
                        "reasoning behind them."
                    ),
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths that were created or modified.",
                },
            },
            "required": ["summary", "files_changed"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

_OUTPUT_CAP = 50 * 1024  # 50 KB cap on tool output


def _safe_path(work_dir: str, path: str) -> str:
    """Resolve a relative path within work_dir. Rejects traversal."""
    root = Path(work_dir).resolve()
    resolved = root.joinpath(path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"Path escapes work directory: {path}")
    return str(resolved)


def _handle_read_file(work_dir: str, inp: dict[str, Any]) -> str:
    path = _safe_path(work_dir, inp["path"])
    offset = max(1, inp.get("offset", 1))
    limit = min(inp.get("limit", 2000), 5000)

    if not os.path.isfile(path):
        return f"Error: file not found: {inp['path']}"

    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error reading {inp['path']}: {e}"

    total = len(lines)
    start = offset - 1
    end = start + limit
    selected = lines[start:end]

    numbered = "".join(
        f"{start + i + 1}\t{line}" for i, line in enumerate(selected)
    )
    header = f"# {inp['path']} ({total} lines total, showing {offset}-{min(offset + len(selected) - 1, total)})\n"
    result = header + numbered
    return result[:_OUTPUT_CAP]


def _handle_write_file(work_dir: str, inp: dict[str, Any]) -> str:
    path = _safe_path(work_dir, inp["path"])
    content = inp["content"]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return f"Error writing {inp['path']}: {e}"

    return f"Wrote {len(content)} bytes to {inp['path']}"


def _handle_edit_file(work_dir: str, inp: dict[str, Any]) -> str:
    path = _safe_path(work_dir, inp["path"])
    old_string = inp["old_string"]
    new_string = inp["new_string"]

    if not os.path.isfile(path):
        return f"Error: file not found: {inp['path']}"

    if old_string == new_string:
        return "Error: old_string and new_string are identical."

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return f"Error reading {inp['path']}: {e}"

    count = content.count(old_string)
    if count == 0:
        return (
            f"Error: old_string not found in {inp['path']}. "
            "Make sure the text matches exactly (including whitespace and indentation)."
        )
    if count > 1:
        return (
            f"Error: old_string found {count} times in {inp['path']}. "
            "Include more surrounding context to make the match unique."
        )

    new_content = content.replace(old_string, new_string, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return f"Edited {inp['path']} — replaced 1 occurrence."


def _handle_list_directory(work_dir: str, inp: dict[str, Any]) -> str:
    rel_path = inp.get("path", ".")
    path = _safe_path(work_dir, rel_path)
    recursive = inp.get("recursive", False)
    max_depth = min(inp.get("max_depth", 3), 5)

    if not os.path.isdir(path):
        return f"Error: directory not found: {rel_path}"

    entries: list[str] = []
    cap = 500

    if recursive:
        base = Path(path)
        for item in sorted(base.rglob("*")):
            if len(entries) >= cap:
                entries.append(f"... (truncated at {cap} entries)")
                break
            rel = item.relative_to(base)
            depth = len(rel.parts)
            if depth > max_depth:
                continue
            # Skip .git internals
            if ".git" in rel.parts:
                continue
            suffix = "/" if item.is_dir() else ""
            entries.append(f"  {'  ' * (depth - 1)}{rel}{suffix}")
    else:
        base = Path(path)
        for item in sorted(base.iterdir()):
            if len(entries) >= cap:
                entries.append(f"... (truncated at {cap} entries)")
                break
            if item.name == ".git":
                continue
            suffix = "/" if item.is_dir() else ""
            entries.append(f"  {item.name}{suffix}")

    header = f"# {rel_path}/ ({len(entries)} entries)\n"
    return header + "\n".join(entries)


def _handle_run_command(work_dir: str, inp: dict[str, Any]) -> str:
    command = inp["command"]
    timeout = min(inp.get("timeout", 60), 120)

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    except Exception as e:
        return f"Error running command: {e}"

    output = ""
    if result.stdout:
        output += result.stdout
    if result.stderr:
        if output:
            output += "\n--- stderr ---\n"
        output += result.stderr

    if not output:
        output = "(no output)"

    if result.returncode != 0:
        output = f"Exit code: {result.returncode}\n{output}"

    return output[:_OUTPUT_CAP]


_TOOL_HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "read_file": _handle_read_file,
    "write_file": _handle_write_file,
    "edit_file": _handle_edit_file,
    "list_directory": _handle_list_directory,
    "run_command": _handle_run_command,
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are an expert software engineer working in a cloned Git repository.
Your task is to implement the following change:

## Task
{task_description}

## Context from prior research
{context_hint}

## Instructions
1. Start by understanding the codebase structure (list_directory, read_file).
2. Plan your changes before editing.
3. Make changes using edit_file (for modifications) or write_file (for new files).
4. After making changes, verify them by re-reading the edited files.
5. When finished, call submit_changes with a summary and the list of files you changed.

## Rules
- Only modify files relevant to the task.
- Do not modify .git/ or any git configuration.
- Keep changes minimal and focused.
- Prefer edit_file over write_file for existing files (it's cheaper on tokens).
- If you cannot complete the task, call submit_changes with a summary explaining what you tried and why it didn't work.
- Do not add unnecessary features, tests, or documentation beyond what was asked.
"""


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run_agent_loop(
    *,
    work_dir: str,
    task_description: str,
    context_hint: str = "",
    model: str = DEFAULT_MODEL,
    budget_dollars: float = 5.0,
    progress_callback: Callable[[str], None] | None = None,
    max_turns: int = 200,
) -> AgentResult:
    """Run the Claude agent loop to make code changes in work_dir.

    Args:
        work_dir: Path to the cloned repository.
        task_description: What the user asked for (from the DDB job row).
        context_hint: Research notes from the outer agent's code tools.
        model: Anthropic model ID.
        budget_dollars: Per-PR cost cap in dollars.
        progress_callback: Called with step names for Slack tracker.
        max_turns: Hard cap on conversation turns.

    Returns:
        AgentResult with summary, files_changed, token usage, and any error.
    """
    client = anthropic.Anthropic(max_retries=3)
    budget = TokenBudget(budget_dollars=budget_dollars, model=model)
    submitted = False
    result = AgentResult(token_budget=budget)

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        task_description=task_description,
        context_hint=context_hint or "No prior research provided. Explore the codebase to understand it before making changes.",
    )

    # System block with prompt caching.
    system = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Begin working on the task."},
    ]

    has_edited = False

    for turn in range(max_turns):
        # Budget enforcement — inject warnings into the next user turn.
        if budget.fraction_used >= 0.95:
            log.warning(
                "budget hard-stop: %.1f%% used ($%.4f / $%.2f)",
                budget.fraction_used * 100,
                budget.cost_dollars,
                budget.budget_dollars,
            )
            if not submitted:
                result.error = (
                    f"Budget exhausted ({budget.fraction_used:.0%} used). "
                    "Agent did not call submit_changes."
                )
            break

        if budget.fraction_used >= 0.85 and messages[-1]["role"] == "user":
            # Inject a budget warning as a system-style user message.
            warn_msg = (
                f"[BUDGET WARNING: You have used {budget.fraction_used:.0%} of your "
                f"budget (${budget.cost_dollars:.4f} / ${budget.budget_dollars:.2f}). "
                "Wrap up your changes and call submit_changes soon.]"
            )
            content = messages[-1]["content"]
            if isinstance(content, str):
                messages[-1]["content"] = f"{warn_msg}\n\n{content}"
            elif isinstance(content, list):
                messages[-1]["content"] = [{"type": "text", "text": warn_msg}] + content

        # API call.
        try:
            response = client.messages.create(
                model=model,
                max_tokens=16384,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
        except anthropic.APIError as e:
            log.exception("Anthropic API error on turn %d", turn)
            result.error = f"API error on turn {turn}: {type(e).__name__}: {e}"
            break

        budget.update(response.usage)
        log.info(
            "turn %d: stop=%s tokens_in=%d tokens_out=%d cost=$%.4f (%.0f%%)",
            turn,
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
            budget.cost_dollars,
            budget.fraction_used * 100,
        )

        # Process the response.
        if response.stop_reason == "end_turn":
            # Agent stopped without calling submit_changes.
            messages.append({"role": "assistant", "content": response.content})
            if not submitted:
                # Extract any final text as the summary.
                final_text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        final_text += block.text
                result.error = "Agent stopped without calling submit_changes."
                result.summary = final_text or "(no final message)"
            break

        if response.stop_reason != "tool_use":
            log.warning("unexpected stop_reason: %s", response.stop_reason)
            result.error = f"Unexpected stop_reason: {response.stop_reason}"
            break

        # Process tool calls.
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input

            if tool_name == "submit_changes":
                # Termination signal.
                result.summary = tool_input.get("summary", "")
                result.files_changed = tool_input.get("files_changed", [])
                submitted = True
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Changes submitted. Do not continue editing.",
                })
                continue

            handler = _TOOL_HANDLERS.get(tool_name)
            if handler is None:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Error: unknown tool '{tool_name}'.",
                    "is_error": True,
                })
                continue

            try:
                output = handler(work_dir, tool_input)
            except Exception as e:  # noqa: BLE001
                log.warning("tool %s raised: %s", tool_name, e)
                output = f"Error: {type(e).__name__}: {e}"

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

            # Track progress for Slack.
            if tool_name in ("write_file", "edit_file") and not has_edited:
                has_edited = True
                if progress_callback:
                    progress_callback("editing")

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        if submitted:
            break
    else:
        # max_turns exceeded.
        if not submitted:
            result.error = f"Agent exceeded {max_turns} turn limit without calling submit_changes."

    log.info(
        "agent loop done: submitted=%s turns=%d cost=$%.4f files=%s error=%s",
        submitted,
        turn + 1 if "turn" in dir() else 0,
        budget.cost_dollars,
        result.files_changed,
        result.error or "(none)",
    )
    return result


# ---------------------------------------------------------------------------
# PR metadata generation (single call, no tools)
# ---------------------------------------------------------------------------

_PR_META_PROMPT = """\
Generate a Git commit message, PR title, and PR body for the following change.

## Task description
{task_description}

## Agent summary
{agent_summary}

## Diff stat
{diff_stat}

## Output format
Respond with EXACTLY this format (no extra text):

COMMIT_MESSAGE:
<one-line commit message, imperative mood, under 72 chars>

PR_TITLE:
<short PR title, under 70 chars>

PR_BODY:
<markdown PR body: 1-3 bullet summary, then a "Changes" section listing what was modified>
"""


def generate_pr_metadata(
    *,
    task_description: str,
    agent_summary: str,
    diff_stat: str,
    model: str = DEFAULT_MODEL,
) -> PRMetadata:
    """Generate commit message + PR title + body from a diff.

    Single non-agentic call — no tools, no loop. Cheap and fast.
    """
    client = anthropic.Anthropic(max_retries=3)
    prompt = _PR_META_PROMPT.format(
        task_description=task_description,
        agent_summary=agent_summary,
        diff_stat=diff_stat,
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        log.warning("PR metadata generation failed: %s", e)
        return PRMetadata(
            title="Agent: code change",
            body=f"## Summary\n{agent_summary}\n\n## Diff\n```\n{diff_stat}\n```",
            commit_message="Apply code changes",
        )

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text += block.text

    return _parse_pr_metadata(text, agent_summary, diff_stat)


def _parse_pr_metadata(text: str, fallback_summary: str, fallback_diff: str) -> PRMetadata:
    """Parse the structured output from the PR metadata call."""
    meta = PRMetadata()

    # Parse COMMIT_MESSAGE:
    if "COMMIT_MESSAGE:" in text:
        after = text.split("COMMIT_MESSAGE:", 1)[1]
        if "PR_TITLE:" in after:
            meta.commit_message = after.split("PR_TITLE:", 1)[0].strip()
        else:
            meta.commit_message = after.strip().split("\n", 1)[0].strip()

    # Parse PR_TITLE:
    if "PR_TITLE:" in text:
        after = text.split("PR_TITLE:", 1)[1]
        if "PR_BODY:" in after:
            meta.title = after.split("PR_BODY:", 1)[0].strip()
        else:
            meta.title = after.strip().split("\n", 1)[0].strip()

    # Parse PR_BODY:
    if "PR_BODY:" in text:
        meta.body = text.split("PR_BODY:", 1)[1].strip()

    # Fallbacks.
    if not meta.commit_message:
        meta.commit_message = "Apply code changes"
    if not meta.title:
        meta.title = "Agent: code change"
    if not meta.body:
        meta.body = f"## Summary\n{fallback_summary}\n\n## Diff\n```\n{fallback_diff}\n```"

    return meta
