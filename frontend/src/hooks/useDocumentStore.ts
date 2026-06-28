import { useCallback, useEffect, useState } from "react";
import { LOCALSTORAGE_KEYS } from "../lib/constants";
import { readJson, writeJson } from "../lib/persistence";
import type { BrowserDocumentEntry, DocumentRecord } from "../api/types";

interface StorePayload {
  v: 1;
  entries: BrowserDocumentEntry[];
}

const MAX_ENTRIES = 100;

function load(): BrowserDocumentEntry[] {
  const data = readJson<StorePayload | null>(LOCALSTORAGE_KEYS.documents, null);
  if (data && data.v === 1 && Array.isArray(data.entries)) {
    return data.entries.filter((e) => e && e.document && typeof e.document.id === "string");
  }
  return [];
}

// Tab-local shared state so the upload queue and history stay in sync.
const listeners = new Set<(entries: BrowserDocumentEntry[]) => void>();
let current: BrowserDocumentEntry[] = load();

function persist(entries: BrowserDocumentEntry[]) {
  const trimmed = entries.slice(0, MAX_ENTRIES);
  current = trimmed;
  writeJson(LOCALSTORAGE_KEYS.documents, { v: 1, entries: trimmed });
  listeners.forEach((fn) => fn(trimmed));
}

export function useDocumentStore() {
  // Re-sync from storage on mount (keeps the source of truth fresh and tests
  // isolated after localStorage is cleared between cases).
  const [entries, setEntries] = useState<BrowserDocumentEntry[]>(() => {
    current = load();
    return current;
  });

  useEffect(() => {
    const listener = (next: BrowserDocumentEntry[]) => setEntries(next);
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  }, []);

  const upsert = useCallback(
    (record: DocumentRecord, requestTraceId: string | null) => {
      const now = new Date().toISOString();
      const existing = current.find((e) => e.document.id === record.id);
      let next: BrowserDocumentEntry[];
      if (existing) {
        next = current.map((e) =>
          e.document.id === record.id
            ? {
                ...e,
                document: record,
                last_checked_at: now,
                request_trace_id: requestTraceId ?? e.request_trace_id,
              }
            : e,
        );
      } else {
        next = [
          {
            document: record,
            request_trace_id: requestTraceId,
            added_at: now,
            last_checked_at: now,
          },
          ...current,
        ];
      }
      persist(next);
    },
    [],
  );

  const updateRecord = useCallback((record: DocumentRecord) => {
    const now = new Date().toISOString();
    persist(
      current.map((e) =>
        e.document.id === record.id
          ? { ...e, document: record, last_checked_at: now }
          : e,
      ),
    );
  }, []);

  const markNotFound = useCallback((documentId: string) => {
    persist(
      current.map((e) =>
        e.document.id === documentId
          ? {
              ...e,
              document: { ...e.document, status: "deleted", error: "Not found" },
              last_checked_at: new Date().toISOString(),
            }
          : e,
      ),
    );
  }, []);

  const remove = useCallback((documentId: string) => {
    persist(current.filter((e) => e.document.id !== documentId));
  }, []);

  return { entries, upsert, updateRecord, markNotFound, remove };
}
