import { apiClient, newTraceId, TIMEOUT_LONG_MS, TIMEOUT_SHORT_MS } from "./client";
import type { DocumentRecord } from "./types";

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
