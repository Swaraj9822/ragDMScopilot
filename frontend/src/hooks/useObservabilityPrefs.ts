import { useCallback, useState } from "react";
import { LOCALSTORAGE_KEYS } from "../lib/constants";
import { readJson, writeJson } from "../lib/persistence";

export type RefreshInterval = 5 | 10 | 30;

export interface ObservabilityPrefs {
  v: 1;
  autoRefresh: boolean;
  intervalSeconds: RefreshInterval;
  hideConsoleTraffic: boolean;
}

const DEFAULTS: ObservabilityPrefs = {
  v: 1,
  autoRefresh: true,
  intervalSeconds: 10,
  hideConsoleTraffic: true,
};

function load(): ObservabilityPrefs {
  const data = readJson<ObservabilityPrefs | null>(
    LOCALSTORAGE_KEYS.observability,
    null,
  );
  if (data && data.v === 1) return { ...DEFAULTS, ...data };
  return DEFAULTS;
}

export function useObservabilityPrefs() {
  const [prefs, setPrefs] = useState<ObservabilityPrefs>(load);

  const update = useCallback((changes: Partial<ObservabilityPrefs>) => {
    setPrefs((prev) => {
      const next = { ...prev, ...changes };
      writeJson(LOCALSTORAGE_KEYS.observability, next);
      return next;
    });
  }, []);

  return { prefs, update };
}
