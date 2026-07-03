import { useCallback, useEffect, useRef, useState } from "react";
import type { CorpusPage, DocumentRecord } from "../api/types";
import { fetchCorpus, type FetchCorpusParams } from "../api/documents";

export interface CorpusListingState {
  /** All documents accumulated across pages. */
  documents: DocumentRecord[];
  /** Whether the initial fetch is in progress (no documents loaded yet). */
  loading: boolean;
  /** Whether a "load more" fetch is in progress. */
  loadingMore: boolean;
  /** Error from the most recent fetch, if any. */
  error: string | null;
  /** Whether there are more pages available. */
  hasMore: boolean;
  /** Load the next page of results. */
  loadMore: () => void;
  /** Reset and re-fetch from the beginning with optional new params. */
  refresh: (params?: FetchCorpusParams) => void;
}

/**
 * Hook for server-paginated corpus listing (R4).
 *
 * - Fetches pages via GET /corpus with cursor navigation
 * - Retains previously loaded documents on error (R4.12)
 * - Provides empty-state detection via documents.length === 0 && !loading
 */
export function useCorpusListing(initialParams: FetchCorpusParams = {}): CorpusListingState {
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  const paramsRef = useRef<FetchCorpusParams>(initialParams);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const doFetch = useCallback(async (cursor: string | null, append: boolean) => {
    if (append) {
      setLoadingMore(true);
    } else {
      setLoading(true);
    }
    setError(null);

    try {
      const page: CorpusPage = await fetchCorpus({
        ...paramsRef.current,
        cursor,
      });

      if (!mountedRef.current) return;

      if (append) {
        setDocuments((prev) => [...prev, ...page.documents]);
      } else {
        setDocuments(page.documents);
      }
      setNextCursor(page.next_cursor);
    } catch (err) {
      if (!mountedRef.current) return;
      // R4.12: retain previously displayed documents on error
      const message =
        err instanceof Error ? err.message : "Failed to load corpus";
      setError(message);
    } finally {
      if (mountedRef.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  // Initial fetch on mount
  useEffect(() => {
    doFetch(null, false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadMore = useCallback(() => {
    if (nextCursor && !loadingMore) {
      doFetch(nextCursor, true);
    }
  }, [nextCursor, loadingMore, doFetch]);

  const refresh = useCallback((params?: FetchCorpusParams) => {
    if (params) {
      paramsRef.current = params;
    }
    setDocuments([]);
    setNextCursor(null);
    doFetch(null, false);
  }, [doFetch]);

  return {
    documents,
    loading,
    loadingMore,
    error,
    hasMore: nextCursor !== null,
    loadMore,
    refresh,
  };
}
