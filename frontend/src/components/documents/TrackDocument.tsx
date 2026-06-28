import { useState } from "react";
import { Search } from "lucide-react";
import { getDocument } from "../../api/documents";
import { ApiError } from "../../api/client";
import { useDocumentStore } from "../../hooks/useDocumentStore";
import { useToast } from "../../hooks/useToast";
import styles from "./TrackDocument.module.css";

export function TrackDocument() {
  const { upsert } = useDocumentStore();
  const { pushToast } = useToast();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleTrack(e: React.FormEvent) {
    e.preventDefault();
    const id = value.trim();
    if (!id) return;
    setBusy(true);
    setError(null);
    try {
      const record = await getDocument(id);
      upsert(record, null);
      pushToast(`Tracking ${record.title}`, "success");
      setValue("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setError("No document found with that ID.");
      } else if (err instanceof ApiError) {
        setError(err.detail);
      } else {
        setError("Could not look up that document.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={styles.wrap} onSubmit={handleTrack}>
      <label htmlFor="track-id" className="field-label">
        Track an existing document
      </label>
      <div className={styles.row}>
        <input
          id="track-id"
          className="input"
          placeholder="Document ID"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button type="submit" className="btn" disabled={busy || !value.trim()}>
          <Search size={14} aria-hidden="true" />
          {busy ? "Looking up…" : "Track"}
        </button>
      </div>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </form>
  );
}
