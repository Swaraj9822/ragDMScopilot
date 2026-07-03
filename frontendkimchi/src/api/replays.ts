import { apiClient, TIMEOUT_LONG_MS, TIMEOUT_SHORT_MS } from "./client";
import type {
  ReplayRun,
  ReplayRunRequest,
} from "./types";

// ---------------------------------------------------------------------------
// Replay runs (R8)
// ---------------------------------------------------------------------------

/** Initiate a replay run. Returns the created run (queued state). */
export function createReplayRun(payload: ReplayRunRequest): Promise<ReplayRun> {
  return apiClient.postJson<ReplayRun>("/replays", payload, {
    timeoutMs: TIMEOUT_LONG_MS,
  });
}

/** Poll the current state of a replay run. */
export function getReplayRun(replayRunId: string): Promise<ReplayRun> {
  return apiClient.get<ReplayRun>(
    `/replays/${encodeURIComponent(replayRunId)}`,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

/** Cancel a queued or running replay run. */
export function cancelReplayRun(replayRunId: string): Promise<void> {
  return apiClient.postJson<void>(
    `/replays/${encodeURIComponent(replayRunId)}/cancel`,
    {},
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

// ---------------------------------------------------------------------------
// Corpus snapshots (R8)
// ---------------------------------------------------------------------------

export interface CorpusSnapshotSummary {
  corpus_snapshot_id: string;
  created_at: string;
  manifest_size: number;
}

/** List available corpus snapshots for selection. */
export function listCorpusSnapshots(): Promise<CorpusSnapshotSummary[]> {
  return apiClient.get<CorpusSnapshotSummary[]>("/corpus-snapshots", {
    timeoutMs: TIMEOUT_SHORT_MS,
  });
}

/** Response body for `POST /corpus-snapshots` — the minted snapshot id. */
export interface CreateCorpusSnapshotResponse {
  corpus_snapshot_id: string;
}

/** Create a new corpus snapshot. Returns only the minted snapshot id. */
export function createCorpusSnapshot(
  scope?: string[],
): Promise<CreateCorpusSnapshotResponse> {
  return apiClient.postJson<CreateCorpusSnapshotResponse>(
    "/corpus-snapshots",
    scope ? { document_ids: scope } : {},
    { timeoutMs: TIMEOUT_LONG_MS },
  );
}
