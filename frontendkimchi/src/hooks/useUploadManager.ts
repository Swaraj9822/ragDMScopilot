import { useCallback, useEffect, useRef, useState } from "react";
import { uploadDocument } from "../api/documents";
import { ApiError } from "../api/client";
import { formatBytes } from "../lib/format";
import { validateFile, getExtension } from "../lib/fileValidation";
import { useDocumentStore } from "./useDocumentStore";
import { useToast } from "./useToast";

export type UploadState =
  | "invalid"
  | "queued"
  | "uploading"
  | "uploaded"
  | "error";

export interface UploadTask {
  id: string;
  fileName: string;
  ext: string;
  sizeLabel: string;
  state: UploadState;
  error: string | null;
  documentId: string | null;
}

const CONCURRENCY = 2;

function newId() {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function useUploadManager() {
  const { upsert } = useDocumentStore();
  const { pushToast } = useToast();
  const [tasks, setTasks] = useState<UploadTask[]>([]);

  const filesRef = useRef<Map<string, File>>(new Map());
  const activeRef = useRef(0);
  const queueRef = useRef<string[]>([]);
  // Latest store/toast handlers kept in refs so pump stays referentially stable.
  const handlersRef = useRef({ upsert, pushToast });
  useEffect(() => {
    handlersRef.current = { upsert, pushToast };
  }, [upsert, pushToast]);

  const patch = useCallback((id: string, changes: Partial<UploadTask>) => {
    setTasks((prev) => prev.map((t) => (t.id === id ? { ...t, ...changes } : t)));
  }, []);

  // Stable pump that drains the queue respecting the concurrency limit.
  const pump = useCallback(() => {
    while (activeRef.current < CONCURRENCY && queueRef.current.length > 0) {
      const id = queueRef.current.shift()!;
      const file = filesRef.current.get(id);
      if (!file) continue;
      activeRef.current += 1;
      patch(id, { state: "uploading" });

      uploadDocument(file)
        .then(({ record, requestTraceId }) => {
          handlersRef.current.upsert(record, requestTraceId);
          patch(id, { state: "uploaded", documentId: record.id, error: null });
          handlersRef.current.pushToast(`Uploaded ${file.name}`, "success");
        })
        .catch((err: unknown) => {
          let message = "Upload failed.";
          if (err instanceof ApiError) {
            message =
              err.status === 413
                ? "This file exceeds the server's upload limit."
                : err.detail;
          } else if (err instanceof Error) {
            message = err.message;
          }
          patch(id, { state: "error", error: message });
          handlersRef.current.pushToast(`Failed to upload ${file.name}`, "error");
        })
        .finally(() => {
          filesRef.current.delete(id);
          activeRef.current -= 1;
          pump();
        });
    }
  }, [patch]);

  const enqueue = useCallback(
    (files: File[]) => {
      const newTasks: UploadTask[] = [];
      for (const file of files) {
        const id = newId();
        const validation = validateFile(file);
        newTasks.push({
          id,
          fileName: file.name,
          ext: getExtension(file.name),
          sizeLabel: formatBytes(file.size),
          state: validation.ok ? "queued" : "invalid",
          error: validation.error,
          documentId: null,
        });
        if (validation.ok) {
          filesRef.current.set(id, file);
          queueRef.current.push(id);
        }
      }
      setTasks((prev) => [...newTasks, ...prev]);
      pump();
    },
    [pump],
  );

  const removeTask = useCallback((id: string) => {
    filesRef.current.delete(id);
    queueRef.current = queueRef.current.filter((q) => q !== id);
    setTasks((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const clearFinished = useCallback(() => {
    setTasks((prev) =>
      prev.filter((t) => t.state === "queued" || t.state === "uploading"),
    );
  }, []);

  return { tasks, enqueue, removeTask, clearFinished };
}
