"use client";

import { useState, useTransition } from "react";

import type { ChannelInfo, EscalationRoute } from "@/lib/types";

import { type SaveResult, saveEscalationRoutes } from "./actions";

type Status =
  | { kind: "idle" }
  | { kind: "pending" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

type Props = {
  tenantId: string;
  initial: EscalationRoute[];
  channels: ChannelInfo[];
};

const EMPTY_ROUTE: EscalationRoute = {
  team_name: "",
  channel_id: "",
  description: "",
  contacts: [],
};

export default function EscalationEditor({ tenantId, initial, channels }: Props) {
  const [routes, setRoutes] = useState<EscalationRoute[]>(initial);
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [draft, setDraft] = useState<EscalationRoute>(EMPTY_ROUTE);
  const [contactInput, setContactInput] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [isPending, startTransition] = useTransition();

  const channelName = (id: string) => {
    const ch = channels.find((c) => c.id === id);
    return ch ? `${ch.is_private ? "* " : "# "}${ch.name}` : id;
  };

  function openEditor(index: number) {
    setDraft({ ...routes[index], contacts: [...routes[index].contacts] });
    setEditingIndex(index);
    setConfirmDelete(false);
    setContactInput("");
  }

  function openNew() {
    setDraft({ ...EMPTY_ROUTE, contacts: [] });
    setEditingIndex(routes.length);
    setConfirmDelete(false);
    setContactInput("");
  }

  function cancelEditor() {
    setEditingIndex(null);
    setConfirmDelete(false);
  }

  function saveDraft() {
    if (!draft.team_name.trim() || !draft.channel_id.trim()) return;
    const updated = [...routes];
    if (editingIndex !== null && editingIndex < routes.length) {
      updated[editingIndex] = draft;
    } else {
      updated.push(draft);
    }
    setRoutes(updated);
    setEditingIndex(null);
    persist(updated);
  }

  function deleteDraft() {
    if (editingIndex === null || editingIndex >= routes.length) return;
    const updated = routes.filter((_, i) => i !== editingIndex);
    setRoutes(updated);
    setEditingIndex(null);
    setConfirmDelete(false);
    persist(updated);
  }

  function addContact() {
    const id = contactInput.trim();
    if (!id || draft.contacts.includes(id)) return;
    setDraft((d) => ({ ...d, contacts: [...d.contacts, id] }));
    setContactInput("");
  }

  function removeContact(id: string) {
    setDraft((d) => ({
      ...d,
      contacts: d.contacts.filter((c) => c !== id),
    }));
  }

  function persist(updated: EscalationRoute[]) {
    setStatus({ kind: "pending" });
    startTransition(async () => {
      const result: SaveResult = await saveEscalationRoutes(tenantId, updated);
      if (result.ok) {
        setStatus({ kind: "saved" });
      } else {
        setStatus({ kind: "error", message: result.error });
      }
    });
  }

  return (
    <div className="space-y-4">
      {routes.length === 0 && editingIndex === null ? (
        <div className="rounded-lg border border-[color:var(--border)] bg-[color:var(--card)] p-6 text-center">
          <p className="text-sm text-[color:var(--muted)]">
            No escalation routes configured. Add a team to get started.
          </p>
        </div>
      ) : null}

      {routes.length > 0 ? (
        <div className="divide-y divide-[color:var(--border)] rounded-lg border border-[color:var(--border)]">
          {routes.map((route, i) => (
            <div
              key={`${route.team_name}-${i}`}
              className="flex items-center justify-between p-4 hover:bg-[color:var(--card)]"
            >
              <div>
                <div className="font-medium text-sm">{route.team_name}</div>
                <p className="mt-0.5 text-xs text-[color:var(--muted)]">
                  &rarr; {channelName(route.channel_id)}
                  {route.contacts.length > 0
                    ? ` \u00b7 ${route.contacts.length} contact${route.contacts.length === 1 ? "" : "s"}`
                    : null}
                </p>
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
        <div className="rounded-lg border border-[color:var(--accent)]/30 bg-white p-5 shadow-sm">
          <h3 className="mb-4 text-sm font-semibold">
            {editingIndex >= routes.length ? "New escalation route" : "Edit route"}
          </h3>
          <div className="space-y-4">
            <label className="block">
              <span className="mb-1 block text-xs font-medium">Team name</span>
              <input
                type="text"
                value={draft.team_name}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, team_name: e.target.value }))
                }
                placeholder="SRE"
                className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
              />
            </label>

            <label className="block">
              <span className="mb-1 block text-xs font-medium">Channel</span>
              {channels.length > 0 ? (
                <select
                  value={draft.channel_id}
                  onChange={(e) =>
                    setDraft((d) => ({ ...d, channel_id: e.target.value }))
                  }
                  className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
                >
                  <option value="">Select a channel...</option>
                  {channels.map((ch) => (
                    <option key={ch.id} value={ch.id}>
                      {ch.is_private ? "* " : "# "}
                      {ch.name}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type="text"
                  value={draft.channel_id}
                  onChange={(e) =>
                    setDraft((d) => ({ ...d, channel_id: e.target.value }))
                  }
                  placeholder="C0123456789"
                  className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
                />
              )}
            </label>

            <label className="block">
              <span className="mb-1 block text-xs font-medium">
                Description{" "}
                <span className="font-normal text-[color:var(--muted)]">(optional)</span>
              </span>
              <input
                type="text"
                value={draft.description}
                onChange={(e) =>
                  setDraft((d) => ({ ...d, description: e.target.value }))
                }
                placeholder="Infrastructure, availability, on-call"
                className="w-full rounded-lg border border-[color:var(--border)] bg-white p-2.5 text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
              />
            </label>

            <div>
              <span className="mb-1 block text-xs font-medium">Contacts</span>
              <p className="mb-2 text-xs text-[color:var(--muted)]">
                Slack user IDs to @mention in escalation messages (e.g. U0123456789).
              </p>
              {draft.contacts.length > 0 ? (
                <ul className="mb-2 flex flex-wrap gap-2">
                  {draft.contacts.map((id) => (
                    <li
                      key={id}
                      className="flex items-center gap-1.5 rounded-full border border-[color:var(--border)] bg-[color:var(--card)] px-2.5 py-1 font-mono text-xs"
                    >
                      {id}
                      <button
                        aria-label={`Remove contact ${id}`}
                        type="button"
                        onClick={() => removeContact(id)}
                        className="text-[color:var(--muted)] hover:text-red-600"
                      >
                        &times;
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="flex gap-2">
                <input
                  aria-label="Slack contact user ID"
                  type="text"
                  value={contactInput}
                  onChange={(e) => setContactInput(e.target.value)}
                  onKeyDown={(e) =>
                    e.key === "Enter" && (e.preventDefault(), addContact())
                  }
                  placeholder="U0123456789"
                  className="flex-1 rounded-lg border border-[color:var(--border)] bg-white p-2.5 font-mono text-sm shadow-sm focus:border-[color:var(--accent)] focus:outline-none focus:ring-2 focus:ring-[color:var(--accent)]/20"
                />
                <button
                  type="button"
                  onClick={addContact}
                  disabled={!contactInput.trim()}
                  className="rounded-full border border-[color:var(--border)] px-4 py-2 text-sm font-medium hover:bg-[color:var(--card)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Add
                </button>
              </div>
            </div>

            <div className="flex items-center gap-3 border-t border-[color:var(--border)] pt-4">
              <button
                type="button"
                onClick={saveDraft}
                disabled={!draft.team_name.trim() || !draft.channel_id.trim()}
                className="rounded-full bg-[color:var(--accent)] px-5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-[color:var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50"
              >
                {editingIndex >= routes.length ? "Add route" : "Save route"}
              </button>
              <button
                type="button"
                onClick={cancelEditor}
                className="rounded-full border border-[color:var(--border)] px-5 py-2 text-sm font-medium hover:bg-[color:var(--card)]"
              >
                Cancel
              </button>
              {editingIndex < routes.length ? (
                confirmDelete ? (
                  <span className="ml-auto flex items-center gap-2 text-sm">
                    <span className="text-red-600">Delete this route?</span>
                    <button
                      type="button"
                      onClick={deleteDraft}
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
      ) : null}

      <div className="flex items-center gap-4">
        <button
          type="button"
          onClick={openNew}
          disabled={editingIndex !== null || isPending}
          className="rounded-full bg-[color:var(--accent)] px-5 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-[color:var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50"
        >
          + Add team
        </button>
        {status.kind === "saved" ? (
          <span aria-live="polite" className="text-sm text-green-600" role="status">
            Saved.
          </span>
        ) : null}
        {status.kind === "error" ? (
          <span aria-live="assertive" className="text-sm text-red-600" role="alert">
            Couldn&apos;t save: {status.message}
          </span>
        ) : null}
      </div>
    </div>
  );
}
