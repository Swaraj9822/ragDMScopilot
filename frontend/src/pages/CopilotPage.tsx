import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { ask } from "../api/copilot";
import { ApiError, NetworkError, TimeoutError } from "../api/client";
import { useCopilotHistory } from "../hooks/useCopilotHistory";
import { useSelectedDocuments } from "../hooks/useSelectedDocuments";
import { useToast } from "../hooks/useToast";
import { Composer } from "../components/copilot/Composer";
import { ConversationView } from "../components/copilot/ConversationView";
import { ContextRail } from "../components/copilot/ContextRail";
import { ExamplePrompts } from "../components/copilot/ExamplePrompts";
import { ConfirmDialog } from "../components/common/ConfirmDialog";
import styles from "./CopilotPage.module.css";

interface CopilotError {
  message: string;
  retriable: boolean;
}

function describeError(error: unknown): CopilotError {
  if (error instanceof ApiError) {
    if (error.status === 503) {
      return {
        message: "The requested AI service is currently unavailable.",
        retriable: true,
      };
    }
    if (error.status === 400) {
      return { message: error.detail, retriable: false };
    }
    return { message: error.detail, retriable: error.status >= 500 };
  }
  if (error instanceof TimeoutError) {
    return { message: "The request timed out. Try again.", retriable: true };
  }
  if (error instanceof NetworkError) {
    return { message: "Network error. Check your connection and retry.", retriable: true };
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return { message: "Stopped waiting for the response.", retriable: true };
  }
  return { message: "Something went wrong.", retriable: true };
}

export default function CopilotPage() {
  const { exchanges, append, clear } = useCopilotHistory();
  const { ids: selectedIds, remove: removeDoc } = useSelectedDocuments();
  const { pushToast } = useToast();

  const [draft, setDraft] = useState("");
  const [includeSql, setIncludeSql] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [error, setError] = useState<CopilotError | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const lastSubmittedRef = useRef<string>("");

  const mutation = useMutation({
    mutationFn: (question: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      return ask({
        question,
        documentIds: selectedIds,
        includeSql,
        signal: controller.signal,
      });
    },
    onSuccess: (result, question) => {
      append({
        id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
        question,
        response: result.response,
        elapsedMs: result.elapsedMs,
        askedAt: new Date().toISOString(),
      });
      setPendingQuestion(null);
      setError(null);
      pushToast("Answer ready", "success");
    },
    onError: (err) => {
      setError(describeError(err));
    },
  });

  function submit(question: string) {
    const trimmed = question.trim();
    if (!trimmed || mutation.isPending) return;
    lastSubmittedRef.current = trimmed;
    setPendingQuestion(trimmed);
    setError(null);
    setDraft("");
    mutation.mutate(trimmed);
  }

  function handleRetry() {
    if (lastSubmittedRef.current) {
      setError(null);
      mutation.mutate(lastSubmittedRef.current);
    }
  }

  function handleStop() {
    abortRef.current?.abort();
  }

  const isEmpty = exchanges.length === 0 && pendingQuestion === null;

  return (
    <div className={styles.layout}>
      <div className={styles.mainColumn}>
        <div className={styles.scrollArea}>
          {isEmpty ? (
            <ExamplePrompts onPick={(text) => setDraft(text)} />
          ) : (
            <ConversationView
              exchanges={exchanges}
              pendingQuestion={pendingQuestion}
              error={error}
              onRetry={handleRetry}
            />
          )}
        </div>
        <div className={styles.composerArea}>
          <Composer
            value={draft}
            onChange={setDraft}
            onSubmit={() => submit(draft)}
            onStop={handleStop}
            submitting={mutation.isPending}
            includeSql={includeSql}
            onToggleSql={setIncludeSql}
          />
        </div>
      </div>

      <ContextRail
        selectedIds={selectedIds}
        onRemove={removeDoc}
        historyCount={exchanges.length}
        onNewSession={() => setConfirmClear(true)}
      />

      <ConfirmDialog
        open={confirmClear}
        title="Start a new session?"
        body="This clears the local question and answer history in this browser. It does not affect the backend."
        confirmLabel="Clear history"
        onConfirm={() => {
          clear();
          setPendingQuestion(null);
          setError(null);
          setConfirmClear(false);
        }}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}
