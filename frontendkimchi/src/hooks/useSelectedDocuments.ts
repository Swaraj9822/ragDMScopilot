import { useCallback, useEffect, useState } from "react";
import { LOCALSTORAGE_KEYS } from "../lib/constants";
import { readJson, writeJson } from "../lib/persistence";

interface SelectedPayload {
  v: 1;
  ids: string[];
}

function load(): string[] {
  const data = readJson<SelectedPayload | null>(
    LOCALSTORAGE_KEYS.selectedDocuments,
    null,
  );
  if (data && data.v === 1 && Array.isArray(data.ids)) {
    return data.ids.filter((id) => typeof id === "string");
  }
  return [];
}

// Shared across hook instances within a tab via a tiny event bus.
const listeners = new Set<(ids: string[]) => void>();
let current: string[] = load();

function broadcast(ids: string[]) {
  current = ids;
  writeJson(LOCALSTORAGE_KEYS.selectedDocuments, { v: 1, ids });
  listeners.forEach((fn) => fn(ids));
}

export function useSelectedDocuments() {
  // Re-sync from storage on mount so the source of truth reflects the latest
  // persisted value (also keeps tests isolated after localStorage is cleared).
  const [ids, setIds] = useState<string[]>(() => {
    current = load();
    return current;
  });

  useEffect(() => {
    const listener = (next: string[]) => setIds(next);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  const add = useCallback((id: string) => {
    if (current.includes(id)) return;
    broadcast([...current, id]);
  }, []);

  const remove = useCallback((id: string) => {
    broadcast(current.filter((x) => x !== id));
  }, []);

  const clear = useCallback(() => broadcast([]), []);

  return { ids, add, remove, clear };
}
