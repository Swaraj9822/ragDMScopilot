import { useState } from "react";
import { AlertTriangle, ChevronDown, FileStack, Library, Loader2 } from "lucide-react";
import type { BrowserDocumentEntry } from "../api/types";
import { replaceDocument, deleteDocument } from "../api/documents";
import { ApiError } from "../api/client";
import { PageHeader } from "../components/common/PageHeader";
import { ConfirmDialog } from "../components/common/ConfirmDialog";
import { UploadDropZone } from "../components/documents/UploadDropZone";
import { UploadQueue } from "../components/documents/UploadQueue";
import { DocumentCard } from "../components/documents/DocumentCard";
import { CorpusDocumentCard } from "../components/documents/CorpusDocumentCard";
import { TrackDocument } from "../components/documents/TrackDocument";
import { useUploadManager } from "../hooks/useUploadManager";
import { useDocumentStore } from "../hooks/useDocumentStore";
import { useCorpusListing } from "../hooks/useCorpusListing";
import { useToast } from "../hooks/useToast";
import styles from "./DocumentsPage.module.css";

export default function DocumentsPage() {
  const { tasks, enqueue, removeTask, clearFinished } = useUploadManager();
  const { entries, upsert, remove } = useDocumentStore();
  const { pushToast } = useToast();
  const corpus = useCorpusListing();

  const [pendingDelete, setPendingDelete] = useState<BrowserDocumentEntry | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function handleReplace(documentId: string, file: File) {
    try {
      const { record, requestTraceId } = await replaceDocument(documentId, file);
      upsert(record, requestTraceId);
      pushToast(`Replacing ${record.title}`, "success");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.status === 413
            ? "This file exceeds the server's upload limit."
            : err.detail
          : "Replace failed.";
      pushToast(message, "error");
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    const { id, title } = pendingDelete.document;
    try {
      await deleteDocument(id);
      remove(id);
      pushToast(`Deleted ${title}`, "success");
      setPendingDelete(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        remove(id);
        pushToast("Document was already removed.", "warning");
        setPendingDelete(null);
      } else {
        pushToast(
          err instanceof ApiError ? err.detail : "Delete failed.",
          "error",
        );
      }
    } finally {
      setDeleting(false);
    }
  }

  const hasUploads = entries.length > 0 || tasks.length > 0;

  return (
    <div>
      <PageHeader
        title="Documents"
        subtitle="Upload supported files and follow their ingestion into Pinecone."
      />

      <div className={`${styles.layout} ${hasUploads ? styles.uploaded : ""}`}>
        <div className={styles.left}>
          <UploadDropZone onFiles={enqueue} />
          <UploadQueue tasks={tasks} onRemove={removeTask} onClearFinished={clearFinished} />
          <TrackDocument />
        </div>

        <div className={styles.right}>
          {/* ── Server corpus listing (R4) ── */}
          <section className={styles.panel}>
            <div className={styles.panelHead}>
              <h2 className={styles.panelTitle}>Corpus</h2>
              <p className={styles.panelDesc}>
                All documents in the backend corpus, fetched from the server.
              </p>
            </div>

            {corpus.loading && corpus.documents.length === 0 ? (
              <div className={styles.corpusLoading} aria-busy="true" aria-label="Loading corpus">
                <Loader2 size={20} className={styles.spinner} aria-hidden="true" />
                <span className="meta">Loading corpus…</span>
              </div>
            ) : corpus.documents.length === 0 && !corpus.error ? (
              <div className={styles.emptyInline}>
                <Library size={28} className={styles.emptyInlineIcon} aria-hidden="true" />
                <p className={styles.emptyInlineTitle}>No documents in the corpus.</p>
                <p className={styles.emptyInlineBody}>
                  No documents match the current view. Upload a file to get started.
                </p>
              </div>
            ) : (
              <>
                <div className={styles.cards}>
                  {corpus.documents.map((doc) => (
                    <CorpusDocumentCard key={doc.id} document={doc} />
                  ))}
                </div>

                {corpus.hasMore && (
                  <button
                    type="button"
                    className={`btn ${styles.loadMoreBtn}`}
                    onClick={corpus.loadMore}
                    disabled={corpus.loadingMore}
                    aria-label="Load more documents"
                  >
                    {corpus.loadingMore ? (
                      <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
                    ) : (
                      <ChevronDown size={14} aria-hidden="true" />
                    )}
                    {corpus.loadingMore ? "Loading…" : "Load more"}
                  </button>
                )}
              </>
            )}

            {corpus.error && (
              <div className={styles.corpusError} role="alert">
                <AlertTriangle size={16} aria-hidden="true" />
                <span>Could not retrieve the corpus. {corpus.error}</span>
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => corpus.refresh()}
                >
                  Retry
                </button>
              </div>
            )}
          </section>

          {/* ── Browser-local uploads ── */}
          <section className={styles.panel}>
            <div className={styles.panelHead}>
              <h2 className={styles.panelTitle}>This browser&apos;s uploads</h2>
              <p className={styles.panelDesc}>
                Records uploaded or tracked in this browser. This is not the full corpus.
              </p>
            </div>
            {entries.length === 0 ? (
              <div className={styles.emptyInline}>
                <FileStack size={28} className={styles.emptyInlineIcon} aria-hidden="true" />
                <p className={styles.emptyInlineTitle}>
                  No uploads saved in this browser yet.
                </p>
                <p className={styles.emptyInlineBody}>
                  Upload a file or track an existing document ID to see it here.
                </p>
              </div>
            ) : (
              <div className={styles.cards}>
                {entries.map((entry) => (
                  <DocumentCard
                    key={entry.document.id}
                    entry={entry}
                    onReplace={handleReplace}
                    onDelete={setPendingDelete}
                    onRemoveLocal={remove}
                  />
                ))}
              </div>
            )}
          </section>
        </div>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        danger
        busy={deleting}
        title="Delete document"
        body={
          pendingDelete
            ? `Delete "${pendingDelete.document.title}" from the retrieval corpus? This removes its indexed chunks and stored document record. This action cannot be undone.`
            : ""
        }
        confirmLabel="Delete"
        onConfirm={confirmDelete}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
