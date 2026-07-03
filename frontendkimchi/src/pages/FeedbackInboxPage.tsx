import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  Inbox,
  Loader2,
  ArrowUpCircle,
  CheckSquare,
} from "lucide-react";
import type { FeedbackContext, FailureCategory, ReviewStatus } from "../api/types";
import {
  fetchFeedback,
  classifyFeedback,
  promoteFeedback,
  resolveFeedback,
  type FetchFeedbackParams,
} from "../api/feedback";
import { ApiError } from "../api/client";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import { RouteBadge } from "../components/common/RouteBadge";
import { useToast } from "../hooks/useToast";
import styles from "./FeedbackInboxPage.module.css";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const FAILURE_CATEGORIES: FailureCategory[] = [
  "Missing knowledge",
  "Retrieval failure",
  "Wrong route",
  "Unsupported answer",
  "SQL problem",
  "Ambiguous question",
];

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All statuses" },
  { value: "unreviewed", label: "Unreviewed" },
  { value: "reviewed", label: "Reviewed" },
  { value: "resolved", label: "Resolved" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function statusClassName(status: string): string {
  switch (status) {
    case "unreviewed":
      return styles.statusUnreviewed;
    case "reviewed":
      return styles.statusReviewed;
    case "resolved":
      return styles.statusResolved;
    default:
      return styles.statusUnreviewed;
  }
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// FeedbackCard
// ---------------------------------------------------------------------------

interface FeedbackCardProps {
  item: FeedbackContext;
  onClassify: (feedbackId: string, category: FailureCategory) => Promise<void>;
  onPromote: (feedbackId: string) => Promise<void>;
  onResolve: (feedbackId: string) => Promise<void>;
}

function FeedbackCard({ item, onClassify, onPromote, onResolve }: FeedbackCardProps) {
  const { feedback, expected_answer, confidence, route, retrieved_chunks, sql } = item;
  const [chunksExpanded, setChunksExpanded] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  async function handleClassify(category: FailureCategory) {
    setBusy("classify");
    try {
      await onClassify(feedback.feedback_id, category);
    } finally {
      setBusy(null);
    }
  }

  async function handlePromote() {
    setBusy("promote");
    try {
      await onPromote(feedback.feedback_id);
    } finally {
      setBusy(null);
    }
  }

  async function handleResolve() {
    setBusy("resolve");
    try {
      await onResolve(feedback.feedback_id);
    } finally {
      setBusy(null);
    }
  }

  return (
    <article className={styles.feedbackCard} aria-label={`Feedback ${feedback.feedback_id}`}>
      {/* Header */}
      <div className={styles.cardHeader}>
        <span className={styles.ratingBadge} aria-label={`Rating ${feedback.rating}`}>
          Rating {feedback.rating}
        </span>
        <span
          className={`${styles.statusBadge} ${statusClassName(feedback.review_status)}`}
          aria-label={`Status: ${feedback.review_status}`}
        >
          {feedback.review_status}
        </span>
        {feedback.failure_category && (
          <span className={styles.statusBadge} style={{ background: "var(--bg-subtle)", color: "var(--text-secondary)", border: "1px solid var(--border-default)" }}>
            {feedback.failure_category}
          </span>
        )}
        <span className={styles.timestamp}>{formatDate(feedback.created_at)}</span>
      </div>

      {/* Body / Context */}
      <div className={styles.cardBody}>
        <div className={styles.field}>
          <span className={styles.fieldLabel}>Comment:</span>
          <span className={feedback.comment ? styles.fieldValue : styles.fieldMuted}>
            {feedback.comment || "No comment"}
          </span>
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>Expected answer:</span>
          <span className={expected_answer ? styles.fieldValue : styles.fieldMuted}>
            {expected_answer || "Not provided"}
          </span>
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>Confidence:</span>
          <span className={confidence ? styles.fieldValue : styles.fieldMuted}>
            {confidence || "N/A"}
          </span>
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>Route:</span>
          {route ? <RouteBadge route={route} /> : <span className={styles.fieldMuted}>N/A</span>}
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>SQL:</span>
          <span className={sql ? styles.fieldValue : styles.fieldMuted}>
            {sql || "None"}
          </span>
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>Retrieved chunks:</span>
          {retrieved_chunks.length > 0 ? (
            <>
              <button
                type="button"
                className={styles.chunksToggle}
                onClick={() => setChunksExpanded(!chunksExpanded)}
                aria-expanded={chunksExpanded}
                aria-label="Toggle retrieved chunks"
              >
                {chunksExpanded ? "Hide" : `Show ${retrieved_chunks.length} chunk(s)`}
              </button>
              {chunksExpanded && (
                <div className={styles.chunksList} role="list" aria-label="Retrieved chunks">
                  {retrieved_chunks.map((chunk, idx) => (
                    <div key={chunk.chunk_id ?? idx} className={styles.chunkItem} role="listitem">
                      <strong>{chunk.title ?? chunk.document_id}</strong> (score: {chunk.score.toFixed(3)})
                      {"\n"}
                      {chunk.text}
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <span className={styles.fieldMuted}>None</span>
          )}
        </div>

        <div className={styles.field}>
          <span className={styles.fieldLabel}>Review status:</span>
          <span className={styles.fieldValue}>{feedback.review_status}</span>
        </div>
      </div>

      {/* Actions */}
      <div className={styles.cardActions}>
        <select
          className={styles.classifySelect}
          value=""
          disabled={busy !== null}
          onChange={(e) => {
            if (e.target.value) {
              handleClassify(e.target.value as FailureCategory);
            }
          }}
          aria-label="Classify feedback"
        >
          <option value="" disabled>
            Classify…
          </option>
          {FAILURE_CATEGORIES.map((cat) => (
            <option key={cat} value={cat}>
              {cat}
            </option>
          ))}
        </select>

        <button
          type="button"
          className="btn btn-sm"
          onClick={handlePromote}
          disabled={busy !== null}
          aria-label="Promote to evaluation set"
        >
          {busy === "promote" ? (
            <Loader2 size={12} className={styles.spinner} aria-hidden="true" />
          ) : (
            <ArrowUpCircle size={12} aria-hidden="true" />
          )}
          Promote
        </button>

        <button
          type="button"
          className="btn btn-sm"
          onClick={handleResolve}
          disabled={busy !== null}
          aria-label="Resolve feedback"
        >
          {busy === "resolve" ? (
            <Loader2 size={12} className={styles.spinner} aria-hidden="true" />
          ) : (
            <CheckSquare size={12} aria-hidden="true" />
          )}
          Resolve
        </button>
      </div>
    </article>
  );
}

// ---------------------------------------------------------------------------
// FeedbackInboxPage
// ---------------------------------------------------------------------------

export default function FeedbackInboxPage() {
  const { pushToast } = useToast();

  const [items, setItems] = useState<FeedbackContext[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<ReviewStatus | "">("");

  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const doFetch = useCallback(async (cursor: string | null, append: boolean, status: ReviewStatus | "") => {
    if (append) {
      setLoadingMore(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      const params: FetchFeedbackParams = { cursor };
      if (status) params.review_status = status as ReviewStatus;

      const page = await fetchFeedback(params);
      if (!mountedRef.current) return;

      if (append) {
        setItems((prev) => [...prev, ...page.items]);
      } else {
        setItems(page.items);
      }
      setNextCursor(page.next_cursor);
    } catch (err) {
      if (!mountedRef.current) return;
      const message = err instanceof Error ? err.message : "Failed to load feedback";
      setError(message);
    } finally {
      if (mountedRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  // Initial fetch + refetch when filter changes
  useEffect(() => {
    setItems([]);
    setNextCursor(null);
    doFetch(null, false, statusFilter);
  }, [statusFilter, doFetch]);

  function loadMore() {
    if (nextCursor && !loadingMore) {
      doFetch(nextCursor, true, statusFilter);
    }
  }

  // Action handlers — update the row, or drop it when it no longer matches the
  // active status filter (otherwise a "reviewed"/"resolved" item lingers in a
  // filtered view it no longer belongs to).
  async function handleClassify(feedbackId: string, category: FailureCategory) {
    try {
      await classifyFeedback(feedbackId, category);
      const nextStatus: ReviewStatus = "reviewed";
      const stillMatches = statusFilter === "" || statusFilter === nextStatus;
      setItems((prev) =>
        prev.flatMap((it) => {
          if (it.feedback.feedback_id !== feedbackId) return [it];
          if (!stillMatches) return [];
          return [
            {
              ...it,
              feedback: {
                ...it.feedback,
                failure_category: category,
                review_status: nextStatus,
              },
            },
          ];
        }),
      );
      pushToast(`Classified as "${category}"`, "success");
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Classification failed";
      pushToast(detail, "error");
    }
  }

  async function handlePromote(feedbackId: string) {
    try {
      await promoteFeedback(feedbackId);
      pushToast("Promoted to evaluation set", "success");
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Promotion failed";
      pushToast(detail, "error");
    }
  }

  async function handleResolve(feedbackId: string) {
    try {
      await resolveFeedback(feedbackId);
      const nextStatus: ReviewStatus = "resolved";
      const stillMatches = statusFilter === "" || statusFilter === nextStatus;
      setItems((prev) =>
        prev.flatMap((it) => {
          if (it.feedback.feedback_id !== feedbackId) return [it];
          if (!stillMatches) return [];
          return [
            { ...it, feedback: { ...it.feedback, review_status: nextStatus } },
          ];
        }),
      );
      pushToast("Marked as resolved", "success");
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : "Resolve failed";
      pushToast(detail, "error");
    }
  }

  return (
    <div className={styles.container}>
      <PageHeader
        title="Feedback Inbox"
        subtitle="Negative-rating feedback with full context. Classify, promote, or resolve items."
      />

      {/* Filter toolbar */}
      <div className={styles.toolbar}>
        <label className={styles.filterLabel} htmlFor="feedback-status-filter">
          Filter by status:
        </label>
        <select
          id="feedback-status-filter"
          className={styles.filterSelect}
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as ReviewStatus | "")}
          aria-label="Filter by review status"
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* Error bar */}
      {error && (
        <div className={styles.errorBar} role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>{error}</span>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => doFetch(null, false, statusFilter)}
          >
            Retry
          </button>
        </div>
      )}

      {/* Loading state */}
      {loading && items.length === 0 && !error && (
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", padding: "var(--space-6) 0" }} aria-busy="true" aria-label="Loading feedback">
          <Loader2 size={20} className={styles.spinner} aria-hidden="true" />
          <span className="meta">Loading feedback…</span>
        </div>
      )}

      {/* Empty state */}
      {!loading && items.length === 0 && !error && (
        <EmptyState
          icon={Inbox}
          title="No feedback items"
          body={statusFilter
            ? `No feedback items with status "${statusFilter}".`
            : "No negative-rating feedback to review."}
        />
      )}

      {/* Feedback list */}
      {items.length > 0 && (
        <div className={styles.feedbackList} role="list" aria-label="Feedback items">
          {items.map((item) => (
            <FeedbackCard
              key={item.feedback.feedback_id}
              item={item}
              onClassify={handleClassify}
              onPromote={handlePromote}
              onResolve={handleResolve}
            />
          ))}
        </div>
      )}

      {/* Load more */}
      {nextCursor !== null && (
        <button
          type="button"
          className={`btn ${styles.loadMoreBtn}`}
          onClick={loadMore}
          disabled={loadingMore}
          aria-label="Load more feedback"
        >
          {loadingMore ? (
            <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
          ) : (
            <ChevronDown size={14} aria-hidden="true" />
          )}
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </div>
  );
}
