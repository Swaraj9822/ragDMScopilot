import { useRef, useState } from "react";
import { ChevronDown, UploadCloud } from "lucide-react";
import { ACCEPT_ATTR, ACCEPTED_EXTENSIONS } from "../../lib/constants";
import styles from "./UploadDropZone.module.css";

interface UploadDropZoneProps {
  onFiles: (files: File[]) => void;
}

export function UploadDropZone({ onFiles }: UploadDropZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [showFormats, setShowFormats] = useState(false);

  function handleFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    onFiles(Array.from(list));
  }

  return (
    <div className={styles.panel}>
      <h2 className={styles.heading}>Add knowledge to the corpus</h2>
      <p className={styles.copy}>
        Files are parsed, chunked, embedded, and indexed for retrieval.
      </p>

      <button
        type="button"
        className={`${styles.dropzone} ${dragging ? styles.dragging : ""}`}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          handleFiles(e.dataTransfer.files);
        }}
      >
        <UploadCloud size={28} aria-hidden="true" />
        <span className={styles.dropTitle}>Drop files or click to browse</span>
        <span className={styles.dropHint}>Up to 10 MiB per file · multiple files supported</span>
      </button>

      <input
        ref={inputRef}
        type="file"
        multiple
        accept={ACCEPT_ATTR}
        className="visually-hidden"
        aria-label="Choose files to upload"
        onChange={(e) => {
          handleFiles(e.target.files);
          e.target.value = "";
        }}
      />

      <div className={styles.formats}>
        <button
          type="button"
          className={styles.formatsToggle}
          onClick={() => setShowFormats((v) => !v)}
          aria-expanded={showFormats}
          id="accepted-formats"
        >
          <ChevronDown
            size={14}
            aria-hidden="true"
            style={{ transform: showFormats ? "rotate(180deg)" : "none" }}
          />
          Accepted formats
        </button>
        {showFormats && (
          <p className={styles.formatList}>
            {ACCEPTED_EXTENSIONS.map((e) => e.toUpperCase()).join(", ")}
          </p>
        )}
      </div>
    </div>
  );
}
