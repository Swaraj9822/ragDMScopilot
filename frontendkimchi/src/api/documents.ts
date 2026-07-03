import { apiClient, newTraceId, TIMEOUT_LONG_MS, TIMEOUT_SHORT_MS } from "./client";
import type { CorpusPage, DocumentHistory, DocumentRecord } from "./types";

export interface UploadResult {
  record: DocumentRecord;
  requestTraceId: string;
}

/** Upload a new document. Returns the accepted record plus the forced trace id. */
export async function uploadDocument(
  file: File,
  signal?: AbortSignal,
): Promise<UploadResult> {
  const traceId = newTraceId();
  const form = new FormData();
  form.append("file", file);
  const record = await apiClient.sendForm<DocumentRecord>("/documents", "POST", form, {
    timeoutMs: TIMEOUT_LONG_MS,
    traceId,
    signal,
  });
  return { record, requestTraceId: traceId };
}

/** Replace an existing document, keeping the same document id. */
export async function replaceDocument(
  documentId: string,
  file: File,
  signal?: AbortSignal,
): Promise<UploadResult> {
  const traceId = newTraceId();
  const form = new FormData();
  form.append("file", file);
  const record = await apiClient.sendForm<DocumentRecord>(
    `/documents/${encodeURIComponent(documentId)}`,
    "PUT",
    form,
    { timeoutMs: TIMEOUT_LONG_MS, traceId, signal },
  );
  return { record, requestTraceId: traceId };
}

export function getDocument(documentId: string): Promise<DocumentRecord> {
  return apiClient.get<DocumentRecord>(
    `/documents/${encodeURIComponent(documentId)}`,
    { timeoutMs: TIMEOUT_SHORT_MS, retries: 1 },
  );
}

export function deleteDocument(documentId: string): Promise<DocumentRecord> {
  return apiClient.delete<DocumentRecord>(
    `/documents/${encodeURIComponent(documentId)}`,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}


// ---------------------------------------------------------------------------
// Document version history (R5)
// ---------------------------------------------------------------------------

/** Fetch version history for a document (versions + events newest-first). */
export function fetchDocumentHistory(documentId: string): Promise<DocumentHistory> {
  return apiClient.get<DocumentHistory>(
    `/documents/${encodeURIComponent(documentId)}/versions`,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

/** Restore a specific version of a document (operator-only). */
export function restoreDocumentVersion(
  documentId: string,
  version: string,
): Promise<void> {
  return apiClient.postJson<void>(
    `/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(version)}/restore`,
    {},
    { timeoutMs: TIMEOUT_LONG_MS },
  );
}

// ---------------------------------------------------------------------------
// Server-paginated corpus listing (R4)
// ---------------------------------------------------------------------------

export interface FetchCorpusParams {
  cursor?: string | null;
  sort_field?: "name" | "owner" | "date";
  sort_direction?: "asc" | "desc";
  status?: string;
  owner?: string;
  search?: string;
}

/** Fetch a page of the backend corpus with optional cursor, sort, filter, and search. */
export function fetchCorpus(params: FetchCorpusParams = {}): Promise<CorpusPage> {
  const query = new URLSearchParams();
  if (params.cursor) query.set("cursor", params.cursor);
  if (params.sort_field) query.set("sort_field", params.sort_field);
  if (params.sort_direction) query.set("sort_direction", params.sort_direction);
  if (params.status) query.set("status", params.status);
  if (params.owner) query.set("owner", params.owner);
  if (params.search) query.set("search", params.search);

  const qs = query.toString();
  const path = `/corpus${qs ? `?${qs}` : ""}`;
  return apiClient.get<CorpusPage>(path, { timeoutMs: TIMEOUT_SHORT_MS });
}
