import { useCallback, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Upload, File, CheckCircle2, XCircle, Loader2, Trash2, RefreshCw } from "lucide-react";
import { api, type DocumentRecord } from "../api";
import styles from "./Documents.module.css";

const ACCEPT = ".pdf,.docx,.doc,.pptx,.csv,.xlsx,.txt,.md,.html,.epub,.rtf,.png,.jpg,.jpeg";

interface UploadItem {
  id: string;
  file: File;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
  doc?: DocumentRecord;
}

export default function Documents() {
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const [tracked, setTracked] = useState<DocumentRecord[]>([]);

  const uploadMutation = useMutation({
    mutationFn: async (item: UploadItem) => {
      setUploads((u) => u.map((x) => x.id === item.id ? { ...x, status: "uploading" } : x));
      return api.uploadDocument(item.file);
    },
    onSuccess: (doc, item) => {
      setUploads((u) => u.map((x) => x.id === item.id ? { ...x, status: "done", doc } : x));
      setTracked((t) => [doc, ...t.filter((d) => d.id !== doc.id)]);
    },
    onError: (err, item) => {
      const msg = err instanceof Error ? err.message : "Upload failed";
      setUploads((u) => u.map((x) => x.id === item.id ? { ...x, status: "error", error: msg } : x));
    },
  });

  const handleFiles = useCallback((files: FileList | File[]) => {
    const items: UploadItem[] = Array.from(files).map((file) => ({
      id: crypto.randomUUID(),
      file,
      status: "pending" as const,
    }));
    setUploads((prev) => [...items, ...prev]);
    items.forEach((item) => uploadMutation.mutate(item));
  }, [uploadMutation]);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault(); setDragging(false);
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  };

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Documents</h1>
      <p className={styles.desc}>Upload files to expand the retrieval corpus. The pipeline parses, chunks, and indexes automatically.</p>

      {/* Drop zone */}
      <div
        className={`${styles.drop} ${dragging ? styles.dropActive : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === "Enter") inputRef.current?.click(); }}
        aria-label="Upload files"
      >
        <Upload size={28} className={styles.dropIcon} />
        <span className={styles.dropTitle}>Drop files or click to browse</span>
        <span className={styles.dropMeta}>PDF, DOCX, PPTX, CSV, images, and more</span>
        <input ref={inputRef} type="file" multiple accept={ACCEPT} onChange={(e) => { if (e.target.files?.length) handleFiles(e.target.files); e.target.value = ""; }} hidden />
      </div>

      {/* Upload queue */}
      {uploads.length > 0 && (
        <div className={styles.queue}>
          <div className={styles.queueHeader}>
            <span className={styles.queueTitle}>Uploads</span>
            <button className="btn btn-sm btn-ghost" onClick={() => setUploads((u) => u.filter((x) => x.status === "uploading" || x.status === "pending"))} type="button">
              Clear completed
            </button>
          </div>
          {uploads.map((item) => (
            <div key={item.id} className={styles.queueItem}>
              {item.status === "uploading" || item.status === "pending" ? <Loader2 size={14} className={styles.spin} /> :
               item.status === "done" ? <CheckCircle2 size={14} style={{ color: "var(--success)" }} /> :
               <XCircle size={14} style={{ color: "var(--danger)" }} />}
              <span className={styles.queueName}>{item.file.name}</span>
              <span className={styles.queueSize}>{(item.file.size / 1024).toFixed(0)} KB</span>
              {item.error && <span className={styles.queueErr}>{item.error}</span>}
            </div>
          ))}
        </div>
      )}

      {/* Tracked documents */}
      {tracked.length > 0 && (
        <div className={styles.library}>
          <h2 className={styles.libTitle}>Recently Uploaded</h2>
          <div className={styles.docList}>
            {tracked.map((doc) => (
              <DocRow key={doc.id} doc={doc} onRefresh={() => {
                api.getDocument(doc.id).then((d) => setTracked((t) => t.map((x) => x.id === d.id ? d : x)));
              }} onDelete={() => {
                api.deleteDocument(doc.id).then(() => setTracked((t) => t.filter((x) => x.id !== doc.id)));
              }} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function DocRow({ doc, onRefresh, onDelete }: { doc: DocumentRecord; onRefresh: () => void; onDelete: () => void }) {
  const isTerminal = ["indexed", "failed", "deleted"].includes(doc.status);
  const statusColor = doc.status === "indexed" ? "var(--success)" : doc.status === "failed" ? "var(--danger)" : "var(--info)";

  return (
    <div className={styles.docRow}>
      <File size={16} style={{ color: "var(--ink-muted)", flexShrink: 0 }} />
      <div className={styles.docInfo}>
        <span className={styles.docName}>{doc.title}</span>
        <span className={styles.docId}>{doc.id.slice(0, 12)}…</span>
      </div>
      <span className="pill" style={{ background: `color-mix(in oklch, ${statusColor} 12%, transparent)`, color: statusColor }}>
        {doc.status}
      </span>
      <div className={styles.docActions}>
        {!isTerminal && (
          <button className="btn btn-ghost btn-icon btn-sm" onClick={onRefresh} type="button" aria-label="Refresh status">
            <RefreshCw size={12} />
          </button>
        )}
        <button className="btn btn-ghost btn-icon btn-sm" onClick={onDelete} type="button" aria-label="Delete" style={{ color: "var(--danger)" }}>
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  );
}
