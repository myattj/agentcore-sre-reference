"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type AutoSaveStatus =
  | { kind: "idle" }
  | { kind: "saving" }
  | { kind: "saved" }
  | { kind: "error"; message: string };

/**
 * Debounced auto-save hook. Watches `data` for changes and persists
 * after `delay` ms of inactivity. Also flushes on unmount if dirty.
 */
export function useAutoSave<T>(
  data: T,
  save: (data: T) => Promise<{ ok: boolean; error?: string }>,
  delay = 1500,
): AutoSaveStatus {
  const [status, setStatus] = useState<AutoSaveStatus>({ kind: "idle" });
  const serialized = JSON.stringify(data);
  const lastSaved = useRef(serialized);
  const dataRef = useRef(data);
  const saveRef = useRef(save);
  const savingRef = useRef(false);

  dataRef.current = data;
  saveRef.current = save;

  const doSave = useCallback(async () => {
    if (savingRef.current) return;
    const snapshot = JSON.stringify(dataRef.current);
    if (snapshot === lastSaved.current) return;
    savingRef.current = true;
    setStatus({ kind: "saving" });
    try {
      const result = await saveRef.current(dataRef.current);
      if (result.ok) {
        lastSaved.current = snapshot;
        setStatus({ kind: "saved" });
      } else {
        setStatus({ kind: "error", message: result.error ?? "unknown error" });
      }
    } catch {
      setStatus({ kind: "error", message: "unexpected error" });
    } finally {
      savingRef.current = false;
    }
  }, []);

  // Debounced save on data change
  useEffect(() => {
    if (serialized === lastSaved.current) return;
    const timer = setTimeout(doSave, delay);
    return () => clearTimeout(timer);
  }, [serialized, delay, doSave]);

  // Auto-clear "saved" after 2s
  useEffect(() => {
    if (status.kind !== "saved") return;
    const timer = setTimeout(() => setStatus({ kind: "idle" }), 2000);
    return () => clearTimeout(timer);
  }, [status.kind]);

  // Flush on unmount if dirty
  useEffect(() => {
    return () => {
      if (
        JSON.stringify(dataRef.current) !== lastSaved.current &&
        !savingRef.current
      ) {
        saveRef.current(dataRef.current);
      }
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return status;
}
