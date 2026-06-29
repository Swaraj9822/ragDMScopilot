import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { getDocument } from "../api/documents";
import { ApiError } from "../api/client";
import type { DocumentRecord } from "../api/types";
import { useDocumentStore } from "./useDocumentStore";

const TERMINAL = new Set(["indexed", "failed", "deleted"]);
const POLL_FAST_MS = 2_000;
const POLL_SLOW_MS = 5_000;
const FAST_WINDOW_MS = 30_000;
const MAX_POLL_MS = 10 * 60_000;

interface UseDocumentPollingArgs {
  documentId: string;
  initialStatus: string;
  /** When false, automatic polling is disabled (manual check only). */
  enabled: boolean;
}

export function useDocumentPolling({
  documentId,
  initialStatus,
  enabled,
}: UseDocumentPollingArgs) {
  const { updateRecord, markNotFound } = useDocumentStore();
  const startedAt = useRef<number>(Date.now());
  const wasEnabled = useRef<boolean>(enabled);

  // When polling transitions from disabled to enabled (for example after an
  // indexed document is replaced and returns to a nonterminal status), restart
  // the ten-minute polling window so we do not immediately give up.
  useEffect(() => {
    if (enabled && !wasEnabled.current) {
      startedAt.current = Date.now();
    }
    wasEnabled.current = enabled;
  }, [enabled]);

  const query = useQuery<DocumentRecord, ApiError>({
    queryKey: ["document", documentId],
    queryFn: () => getDocument(documentId),
    enabled,
    refetchInterval: (q) => {
      const status = q.state.data?.status ?? initialStatus;
      if (TERMINAL.has(status)) return false;
      const elapsed = Date.now() - startedAt.current;
      if (elapsed > MAX_POLL_MS) return false;
      return elapsed < FAST_WINDOW_MS ? POLL_FAST_MS : POLL_SLOW_MS;
    },
    retry: false,
  });

  useEffect(() => {
    if (query.data) updateRecord(query.data);
  }, [query.data, updateRecord]);

  useEffect(() => {
    if (query.error instanceof ApiError && query.error.status === 404) {
      markNotFound(documentId);
    }
  }, [query.error, documentId, markNotFound]);

  const elapsed = Date.now() - startedAt.current;
  const pollingTimedOut =
    enabled && elapsed > MAX_POLL_MS && !TERMINAL.has(query.data?.status ?? initialStatus);

  return {
    record: query.data,
    isChecking: query.isFetching,
    error: query.error,
    pollingTimedOut,
    checkNow: () => {
      startedAt.current = Date.now();
      return query.refetch();
    },
  };
}
