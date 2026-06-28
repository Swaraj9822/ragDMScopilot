import { useCallback, useState } from "react";
import { LOCALSTORAGE_KEYS } from "../lib/constants";
import { readJson, writeJson } from "../lib/persistence";
import type { UnifiedQueryResponse } from "../api/types";

export interface CopilotExchange {
  id: string;
  question: string;
  response: UnifiedQueryResponse;
  elapsedMs: number;
  askedAt: string;
}

interface HistoryPayload {
  v: 1;
  items: CopilotExchange[];
}

const MAX_ITEMS = 20;

function load(): CopilotExchange[] {
  const data = readJson<HistoryPayload | null>(
    LOCALSTORAGE_KEYS.copilotHistory,
    null,
  );
  if (data && data.v === 1 && Array.isArray(data.items)) return data.items;
  return [];
}

function persist(items: CopilotExchange[]) {
  writeJson(LOCALSTORAGE_KEYS.copilotHistory, { v: 1, items: items.slice(-MAX_ITEMS) });
}

export function useCopilotHistory() {
  const [exchanges, setExchanges] = useState<CopilotExchange[]>(load);

  const append = useCallback((exchange: CopilotExchange) => {
    setExchanges((prev) => {
      const next = [...prev, exchange].slice(-MAX_ITEMS);
      persist(next);
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setExchanges([]);
    persist([]);
  }, []);

  return { exchanges, append, clear };
}
