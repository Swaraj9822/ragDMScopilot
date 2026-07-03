import { apiClient, TIMEOUT_SHORT_MS } from "./client";
import type { FeedbackContext, FailureCategory, ReviewStatus } from "./types";

// ---------------------------------------------------------------------------
// Feedback inbox API (R6)
// ---------------------------------------------------------------------------

export interface FeedbackPage {
  items: FeedbackContext[];
  next_cursor: string | null;
}

export interface FetchFeedbackParams {
  cursor?: string | null;
  review_status?: ReviewStatus | null;
}

/** Fetch a page of negative-rating feedback items (operator-only). */
export function fetchFeedback(params: FetchFeedbackParams = {}): Promise<FeedbackPage> {
  const query = new URLSearchParams();
  if (params.cursor) query.set("cursor", params.cursor);
  if (params.review_status) query.set("review_status", params.review_status);

  const qs = query.toString();
  const path = `/feedback${qs ? `?${qs}` : ""}`;
  return apiClient.get<FeedbackPage>(path, { timeoutMs: TIMEOUT_SHORT_MS });
}

/** Classify a feedback item with one of the six failure categories. */
export function classifyFeedback(
  feedbackId: string,
  category: FailureCategory,
): Promise<void> {
  return apiClient.postJson<void>(
    `/feedback/${encodeURIComponent(feedbackId)}/classify`,
    { category },
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

/** Promote a feedback item to the evaluation set. */
export function promoteFeedback(feedbackId: string): Promise<void> {
  return apiClient.postJson<void>(
    `/feedback/${encodeURIComponent(feedbackId)}/promote`,
    {},
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

/** Resolve a feedback item (sets review_status to resolved). */
export function resolveFeedback(feedbackId: string): Promise<void> {
  return apiClient.postJson<void>(
    `/feedback/${encodeURIComponent(feedbackId)}/resolve`,
    {},
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}
