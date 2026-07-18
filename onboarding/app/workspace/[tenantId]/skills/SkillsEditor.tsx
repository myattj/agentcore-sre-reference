"use client";

import { useMemo, useRef, useState, useTransition } from "react";

import { KNOWN_CATALOG_TOOLS, type SkillDef, type TenantConfig } from "@/lib/types";

import { type SaveSkillsResult, saveSkills } from "./actions";

type Status =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

type Props = {
  tenantId: string;
  initial: TenantConfig;
};

const EMPTY_SKILL: SkillDef = {
  trigger: "",
  name: "",
  prompt_template: "",
  required_tools: [],
  channels: [],
};

/** Detect unfilled `[placeholder]` slots in a prompt template. */
const BLANK_SLOT_RE = /\[[^\]]{2,}\]/g;

function hasUnfilledSlots(template: string): boolean {
  return BLANK_SLOT_RE.test(template);
}

/** Extract unique slot markers from a template. */
function extractSlots(template: string): { marker: string; label: string }[] {
  const seen = new Set<string>();
  const slots: { marker: string; label: string }[] = [];
  for (const m of template.matchAll(BLANK_SLOT_RE)) {
    if (seen.has(m[0])) continue;
    seen.add(m[0]);
    const inner = m[0].slice(1, -1);
    slots.push({
      marker: m[0],
      label: inner.charAt(0).toUpperCase() + inner.slice(1),
    });
  }
  return slots;
}

function isSlashCommand(trigger: string): boolean {
  return trigger.startsWith("/");
}

// ── Skill templates ─────────────────────────────────────────────────

type SkillTemplate = {
  label: string;
  description: string;
  skill: SkillDef;
};

const SKILL_TEMPLATES: SkillTemplate[] = [
  {
    label: "Escalation assist",
    description:
      "Guides users through escalating an issue — identifies the team, gathers context, asks for confirmation.",
    skill: {
      trigger: "(?i)escalate\\s+to\\s+",
      name: "escalation-assist",
      prompt_template:
        "The user wants to escalate an issue. Help them:\n" +
        "1. Identify the correct team from the escalation routing table\n" +
        "2. Gather context from the current thread if available\n" +
        "3. Draft an escalation summary with: what happened, what was checked, current impact\n" +
        "4. Ask the user to confirm before posting via the escalate tool\n\n" +
        "Do NOT escalate without user confirmation.",
      required_tools: ["escalate", "read_thread_context"],
      channels: [],
    },
  },
];

// ── Main editor ─────────────────────────────────────────────────────

export default function SkillsEditor({ tenantId, initial }: Props) {
  const [skills, setSkills] = useState<SkillDef[]>(initial.skills);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [draft, setDraft] = useState<SkillDef>(EMPTY_SKILL);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [isPending, startTransition] = useTransition();
  const dragIndex = useRef<number | null>(null);

  const allowedTools = initial.catalog.allowed_tools;

  function openEditor(index: number) {
    setDraft({ ...skills[index] });
    setEditingIndex(index);
    setConfirmDelete(false);
  }

  function openNew() {
    setDraft({ ...EMPTY_SKILL });
    setEditingIndex(skills.length);
    setConfirmDelete(false);
  }

  function openFromTemplate(template: SkillTemplate) {
    setDraft({ ...template.skill, required_tools: [...template.skill.required_tools] });
    setEditingIndex(skills.length);
    setConfirmDelete(false);
  }

  function cancelEditor() {
    setEditingIndex(null);
    setConfirmDelete(false);
  }

  function saveDraft(resolved: SkillDef) {
    if (!resolved.name.trim() || !resolved.trigger.trim() || !resolved.prompt_template.trim()) return;
    const updated = [...skills];
    if (editingIndex !== null && editingIndex < skills.length) {
      updated[editingIndex] = resolved;
    } else {
      updated.push(resolved);
    }
    setSkills(updated);
    setEditingIndex(null);
    persistSkills(updated);
  }

  function deleteDraft() {
    if (editingIndex === null || editingIndex >= skills.length) return;
    const updated = skills.filter((_, i) => i !== editingIndex);
    setSkills(updated);
    setEditingIndex(null);
    setConfirmDelete(false);
    persistSkills(updated);
  }

  function toggleRequiredTool(toolId: string) {
    setDraft((d) => ({
      ...d,
      required_tools: d.required_tools.includes(toolId)
        ? d.required_tools.filter((t) => t !== toolId)
        : [...d.required_tools, toolId],
    }));
  }

  function persistSkills(updated: SkillDef[]) {
    setStatus({ kind: "pending" });
    startTransition(async () => {
      const result: SaveSkillsResult = await saveSkills(tenantId, updated);
      if (result.ok) {
        setStatus({ kind: "saved" });
      } else {
        setStatus({ kind: "error", message: result.error });
      }
    });
  }

  function handleDragStart(index: number) {
    dragIndex.current = index;
  }

  function handleDragOver(e: React.DragEvent, index: number) {
    e.preventDefault();
    if (dragIndex.current === null || dragIndex.current === index) return;
    const updated = [...skills];
    const [moved] = updated.splice(dragIndex.current, 1);
    updated.splice(index, 0, moved);
    dragIndex.current = index;
    setSkills(updated);
  }

  function handleDragEnd() {
    if (dragIndex.current !== null) {
      persistSkills(skills);
      dragIndex.current = null;
    }
  }

  function isTemplateAdded(template: SkillTemplate): boolean {
    return skills.some((s) => s.trigger === template.skill.trigger);
  }

  const hasSkillsWithBlanks = skills.some((s) => hasUnfilledSlots(s.prompt_template));

  return (
    <div className="space-y-4">
      {skills.length === 0 && editingIndex === null ? (
        <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-8 text-center">
          <h2 className="mb-2 text-base font-semibold">No skills yet</h2>
          <p className="mx-auto mb-4 max-w-md text-sm text-[color:var(--muted)]">
            Skills are tenant-wide workflows triggered by slash commands or
            pattern matches. For team-specific behaviors (on-call briefings,
            triage), configure channel personas instead.
          </p>
        </div>
      ) : null}

      {skills.length > 0 ? (
        <div className="divide-y divide-[color:var(--border)] rounded-lg border border-[color:var(--border)]">
          {skills.map((skill, i) => (
            <div
              key={`${skill.trigger}-${i}`}
              draggable={editingIndex === null}
              onDragStart={() => handleDragStart(i)}
              onDragOver={(e) => handleDragOver(e, i)}
              onDragEnd={handleDragEnd}
              className="flex cursor-grab items-center justify-between p-4 hover:bg-[color:var(--card)] active:cursor-grabbing"
            >
              <div className="flex items-center gap-3">
                <span className="text-[color:var(--muted)]" title="Drag to reorder">
                  &#x2630;
                </span>
                <div>
                  <div className="flex items-center gap-2">
                    <code className="rounded bg-[color:var(--card)] px-1.5 py-0.5 font-mono text-xs">
                      {skill.trigger}
                    </code>
                    <span className="text-[10px] text-[color:var(--muted)]">
                      {isSlashCommand(skill.trigger) ? "slash command" : "regex"}
                    </span>
                    {hasUnfilledSlots(skill.prompt_template) ? (
                      <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
                        needs setup
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-0.5 text-sm text-[color:var(--muted)]">
                    {skill.name}
                    {skill.required_tools.length > 0
                      ? ` \u00b7 ${skill.required_tools.length} required tool${skill.required_tools.length === 1 ? "" : "s"}`
                      : null}
                    {skill.channels.length > 0
                      ? ` \u00b7 ${skill.channels.length} channel${skill.channels.length === 1 ? "" : "s"}`
                      : null}
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => openEditor(i)}
                disabled={editingIndex !== null}
                className="rounded-md border border-[color:var(--border)] px-3 py-1 text-xs font-medium hover:bg-white disabled:opacity-40"
              >
                Edit
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {editingIndex !== null ? (
        <SkillEditorPanel
          draft={draft}
          setDraft={setDraft}
          isNew={editingIndex >= skills.length}
          allowedTools={allowedTools}
          confirmDelete={confirmDelete}
          setConfirmDelete={setConfirmDelete}
          onSave={saveDraft}
          onCancel={cancelEditor}
          onDelete={deleteDraft}
          onToggleTool={toggleRequiredTool}
        />
      ) : null}

      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={openNew}
          disabled={editingIndex !== null || isPending}
          className="rounded-full bg-[color:var(--accent)] px-5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-[color:var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          + Add skill
        </button>
        {status.kind === "saved" ? (
          <span aria-live="polite" className="text-sm text-green-600" role="status">
            Saved.
          </span>
        ) : null}
        {status.kind === "pending" ? (
          <span aria-live="polite" className="text-sm text-[color:var(--muted)]" role="status">
            Saving...
          </span>
        ) : null}
        {status.kind === "error" ? (
          <span aria-live="assertive" className="text-sm text-red-600" role="alert">
            Couldn&apos;t save: {status.message}
          </span>
        ) : null}
      </div>

      {/* Skill templates */}
      {editingIndex === null ? (
        <div className="mt-6 border-t border-[color:var(--border)] pt-6">
          <h2 className="mb-1 text-sm font-medium">Templates</h2>
          <p className="mb-4 text-xs text-[color:var(--muted)]">
            Pre-built tenant-wide workflows you can add with one click.
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {SKILL_TEMPLATES.map((tpl) => {
              const added = isTemplateAdded(tpl);
              return (
                <div
                  key={tpl.label}
                  className="rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-4"
                >
                  <div className="mb-1 text-sm font-medium">{tpl.label}</div>
                  <p className="mb-3 text-xs text-[color:var(--muted)]">
                    {tpl.description}
                  </p>
                  <button
                    type="button"
                    onClick={() => openFromTemplate(tpl)}
                    disabled={added}
                    className="rounded-full border border-[color:var(--border)] px-4 py-1.5 text-xs font-medium hover:bg-white disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {added ? "Already added" : "Use template"}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {hasSkillsWithBlanks ? (
        <p className="mt-8 border-t border-[color:var(--border)] pt-6 text-xs text-amber-700">
          Some skills have blank <code>[placeholder]</code> slots — fill
          them in or remove them.
        </p>
      ) : null}
    </div>
  );
}

// ── Inline editor panel ─────────────────────────────────────────────

function SkillEditorPanel({
  draft,
  setDraft,
  isNew,
  allowedTools,
  confirmDelete,
  setConfirmDelete,
  onSave,
  onCancel,
  onDelete,
  onToggleTool,
}: {
  draft: SkillDef;
  setDraft: React.Dispatch<React.SetStateAction<SkillDef>>;
  isNew: boolean;
  allowedTools: string[];
  confirmDelete: boolean;
  setConfirmDelete: (v: boolean) => void;
  onSave: (resolved: SkillDef) => void;
  onCancel: () => void;
  onDelete: () => void;
  onToggleTool: (toolId: string) => void;
}) {
  const triggerType = isSlashCommand(draft.trigger) ? "slash" : "regex";
  const [showTemplate, setShowTemplate] = useState(false);

  // Extract slots from the raw template and track their filled values.
  // Slots are snapshotted from the initial draft so they don't vanish
  // as the user types into the raw textarea.
  const slots = useMemo(
    () => extractSlots(draft.prompt_template),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [], // snapshot on mount only
  );
  const [slotValues, setSlotValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(slots.map((s) => [s.marker, ""])),
  );

  const allSlotsFilled = slots.length === 0 || slots.every(
    (s) => (slotValues[s.marker] ?? "").trim().length > 0,
  );

  /** Build the final template with slot values substituted in. */
  function resolveTemplate(): string {
    let tpl = draft.prompt_template;
    for (const { marker } of slots) {
      const val = slotValues[marker];
      if (val?.trim()) {
        tpl = tpl.replaceAll(marker, val.trim());
      }
    }
    return tpl;
  }

  const valid =
    draft.name.trim().length > 0 &&
    draft.trigger.trim().length > 0 &&
    draft.prompt_template.trim().length > 0 &&
    allSlotsFilled;

  function handleSave() {
    if (!valid) return;
    // Filter out empty channel entries before saving
    const cleanChannels = draft.channels.filter((c) => c.trim().length > 0);
    onSave({ ...draft, prompt_template: resolveTemplate(), channels: cleanChannels });
  }

  return (
    <div className="rounded-lg border border-[color:var(--accent)]/30 bg-white p-5 shadow-sm">
      <h2 className="mb-4 text-sm font-semibold">
        {isNew ? "New skill" : "Edit skill"}
      </h2>
      <div className="space-y-4">
        <label className="block">
          <span className="mb-1 block text-xs font-medium">Name</span>
          <input
            type="text"
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            placeholder="oncall-briefing"
            className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
          />
        </label>

        <label className="block">
          <span className="mb-1 block text-xs font-medium">Trigger</span>
          <input
            type="text"
            value={draft.trigger}
            onChange={(e) => setDraft((d) => ({ ...d, trigger: e.target.value }))}
            placeholder="/oncall-start"
            className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
          />
          <span className="mt-1 block text-xs text-[color:var(--muted)]">
            {triggerType === "slash"
              ? "Slash command \u2014 case-insensitive prefix match"
              : "Regex pattern \u2014 matches anywhere in the message"}
          </span>
        </label>

        {/* Slot fields — extracted from [bracketed markers] in the template */}
        {slots.length > 0 ? (
          <div className="space-y-3 rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-4">
            <p className="text-xs font-medium">Fill in for your workspace</p>
            {slots.map((slot) => (
              <label key={slot.marker} className="block">
                <span className="mb-1 block text-xs font-medium">
                  {slot.label}
                </span>
                <input
                  type="text"
                  value={slotValues[slot.marker] ?? ""}
                  onChange={(e) =>
                    setSlotValues((v) => ({
                      ...v,
                      [slot.marker]: e.target.value,
                    }))
                  }
                  placeholder={`e.g. #${slot.label.toLowerCase().replace(/\s+/g, "-")}`}
                  className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
                />
              </label>
            ))}
          </div>
        ) : null}

        {/* Collapsible raw template */}
        <div>
          <button
            type="button"
            onClick={() => setShowTemplate((v) => !v)}
            className="flex items-center gap-1 text-xs font-medium text-[color:var(--muted)] hover:text-[color:var(--foreground)]"
          >
            <span className={`inline-block transition-transform ${showTemplate ? "rotate-90" : ""}`}>
              &#9654;
            </span>
            {slots.length > 0 ? "Edit template" : "Prompt template"}
          </button>
          {showTemplate || slots.length === 0 ? (
            <div className="mt-2">
              <textarea
                value={draft.prompt_template}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, prompt_template: e.target.value }))
                }
                rows={6}
                placeholder="The user is starting an on-call shift. Execute a comprehensive briefing..."
                className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
              />
              <span className="mt-1 block text-xs text-[color:var(--muted)]">
                Placeholders: {"{user_id}"} {"{channel_id}"} {"{thread_id}"}{" "}
                {"{workspace_id}"}
              </span>
            </div>
          ) : null}
        </div>

        <div>
          <span className="mb-2 block text-xs font-medium">Required tools</span>
          <p className="mb-2 text-xs text-[color:var(--muted)]">
            These tools will be available when this skill triggers, even if
            not in the base tool set.
          </p>
          <ul className="space-y-1.5">
            {KNOWN_CATALOG_TOOLS.map((tool) => {
              const isRequired = draft.required_tools.includes(tool.id);
              const notInCatalog = !allowedTools.includes(tool.id);
              return (
                <li key={tool.id}>
                  <label className="flex cursor-pointer items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={isRequired}
                      onChange={() => onToggleTool(tool.id)}
                      className="h-3.5 w-3.5 cursor-pointer rounded border-[color:var(--border)] text-[color:var(--accent)] focus:ring-[color:var(--accent)]"
                    />
                    <span>{tool.label}</span>
                    {isRequired && notInCatalog ? (
                      <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-700">
                        not in base catalog
                      </span>
                    ) : null}
                  </label>
                </li>
              );
            })}
          </ul>
        </div>

        <div>
          <span className="mb-2 block text-xs font-medium">Channels</span>
          <p className="mb-2 text-xs text-[color:var(--muted)]">
            By default, skills fire in all channels. Restrict to specific
            channels if needed.
          </p>
          <label className="mb-2 flex cursor-pointer items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={draft.channels.length > 0}
              onChange={() =>
                setDraft((d) => ({
                  ...d,
                  channels: d.channels.length > 0 ? [] : [""],
                }))
              }
              className="h-3.5 w-3.5 cursor-pointer rounded border-[color:var(--border)] text-[color:var(--accent)] focus:ring-[color:var(--accent)]"
            />
            <span>Restrict to specific channels</span>
          </label>
          {draft.channels.length > 0 ? (
            <div className="space-y-2">
              {draft.channels.map((ch, ci) => (
                <div key={ci} className="flex items-center gap-2">
                  <input
                    aria-label={`Channel ID ${ci + 1}`}
                    type="text"
                    value={ch}
                    onChange={(e) =>
                      setDraft((d) => {
                        const updated = [...d.channels];
                        updated[ci] = e.target.value;
                        return { ...d, channels: updated };
                      })
                    }
                    placeholder="C_CHANNEL_ID"
                    className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
                  />
                  <button
                    type="button"
                    onClick={() =>
                      setDraft((d) => ({
                        ...d,
                        channels: d.channels.filter((_, i) => i !== ci),
                      }))
                    }
                    className="shrink-0 text-sm text-red-500 hover:text-red-700"
                  >
                    Remove
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={() =>
                  setDraft((d) => ({ ...d, channels: [...d.channels, ""] }))
                }
                className="text-xs font-medium text-[color:var(--accent)] hover:underline"
              >
                + Add channel
              </button>
            </div>
          ) : null}
        </div>

        <div className="flex items-center gap-3 border-t border-[color:var(--border)] pt-4">
          <button
            type="button"
            onClick={handleSave}
            disabled={!valid}
            className="rounded-full bg-[color:var(--accent)] px-5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-[color:var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isNew ? "Add skill" : "Save skill"}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="rounded-full border border-[color:var(--border)] px-5 py-2 text-sm font-medium hover:bg-[color:var(--card)]"
          >
            Cancel
          </button>
          {!isNew ? (
            confirmDelete ? (
              <span className="ml-auto flex items-center gap-2 text-sm">
                <span className="text-red-600">Delete this skill?</span>
                <button
                  type="button"
                  onClick={onDelete}
                  className="rounded-full bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-700"
                >
                  Confirm
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmDelete(false)}
                  className="rounded-full border border-[color:var(--border)] px-3 py-1 text-xs font-medium hover:bg-[color:var(--card)]"
                >
                  Cancel
                </button>
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setConfirmDelete(true)}
                className="ml-auto text-sm text-red-600 hover:text-red-700"
              >
                Delete
              </button>
            )
          ) : null}
        </div>
      </div>
    </div>
  );
}
