import { useState } from "react";
import { FileStack } from "lucide-react";
import type { BrowserDocumentEntry } from "../api/types";
import { replaceDocument, deleteDocument } from "../api/documents";
import { ApiError } from "../api/client";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import { ConfirmDialog } from "../components/common/ConfirmDialog";
import { UploadDropZone } from "../components/documents/UploadDropZone";
import { UploadQueue } from "../components/documents/UploadQueue";
import { DocumentCard } from "../components/documents/DocumentCard";
import { TrackDocument } from "../components/documents/TrackDocument";
import { useUploadManager } from "../hooks/useUploadManager";
import { useDocumentStore } from "../hooks/useDocumentStore";
import { useToast } from "../hooks/useToast";
import styles from "./DocumentsPage.module.css";

export default function DocumentsPage() {
  const { tasks, enqueue, removeTask, clearFinished } = useUploadManager();
  const { entries, upsert, remove } = useDocumentStore();
  const { pushToast } = useToast();

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
          <h2 className={styles.sectionTitle}>This browser's uploads</h2>
          <p className="meta" style={{ marginBottom: "var(--space-3)" }}>
            Records uploaded or tracked in this browser. This is not the full corpus.
          </p>
          {entries.length === 0 ? (
            <EmptyState
              icon={FileStack}
              title="No uploads saved in this browser yet."
              body="Upload a file or track an existing document ID to see it here."
            />
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
