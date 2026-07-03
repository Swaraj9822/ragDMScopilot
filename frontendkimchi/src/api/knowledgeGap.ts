import { apiClient, TIMEOUT_LONG_MS } from "./client";
import type { KnowledgeGapMap } from "./types";

// ---------------------------------------------------------------------------
// Knowledge gap map (R11)
// ---------------------------------------------------------------------------

/** Generate a knowledge gap map from eligible query outcomes (operator-only). */
export function generateKnowledgeGapMap(): Promise<KnowledgeGapMap> {
  return apiClient.postJson<KnowledgeGapMap>(
    "/knowledge-gap-map",
    {},
    { timeoutMs: TIMEOUT_LONG_MS },
  );
}
