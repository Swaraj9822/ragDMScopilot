import { useCallback, useState } from "react";
import { LOCALSTORAGE_KEYS } from "../lib/constants";
import { readJson, writeJson } from "../lib/persistence";
import type { ObsView } from "../components/observability/ViewSwitch";

export type RefreshInterval = 5 | 10 | 30;

export interface ObservabilityPrefs {
  v: 1;
  autoRefresh: boolean;
  intervalSeconds: RefreshInterval;
  hideConsoleTraffic: boolean;
  /** Last selected view tab (Traces / Individual Query / Logs), restored across visits. */
  lastView: ObsView;
}

const DEFAULTS: ObservabilityPrefs = {
  v: 1,
  autoRefresh: true,
  intervalSeconds: 10,
  hideConsoleTraffic: true,
  lastView: "traces",
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
