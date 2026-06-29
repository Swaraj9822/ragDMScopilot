import { useEffect, useRef, useState } from "react";
import { askStream } from "../api/copilot";
import { ApiError, NetworkError, TimeoutError } from "../api/client";
import { useCopilotHistory } from "../hooks/useCopilotHistory";
import { useSelectedDocuments } from "../hooks/useSelectedDocuments";
import { useToast } from "../hooks/useToast";
import { Composer } from "../components/copilot/Composer";
import { ConversationView } from "../components/copilot/ConversationView";
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
  const { ids: selectedIds } = useSelectedDocuments();
  const { pushToast } = useToast();

  const [draft, setDraft] = useState("");
  const [includeSql, setIncludeSql] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [error, setError] = useState<CopilotError | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  // Live streaming state for the in-flight answer.
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [streamStage, setStreamStage] = useState<string | null>(null);
  const [streamRoute, setStreamRoute] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const lastSubmittedRef = useRef<string>("");
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Keep the newest answer in view: scroll to the bottom when a question is
  // submitted and as the answer streams in. block: "end" stops just above the
  // sticky composer rather than yanking past it.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [pendingQuestion, streamText, exchanges.length]);

  async function runStream(question: string) {
    const controller = new AbortController();
    abortRef.current = controller;
    const started = performance.now();
    setStreaming(true);
    setStreamText("");
    setStreamStage(null);
    setStreamRoute(null);
    setError(null);

    try {
      await askStream(
        {
          question,
          documentIds: selectedIds,
          includeSql,
          signal: controller.signal,
        },
        {
          onMeta: (meta) => setStreamRoute(meta.route),
          onStatus: (stage) => setStreamStage(stage),
          onDelta: (text) => setStreamText((prev) => prev + text),
          onFinal: (response) => {
            append({
              id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
              question,
              response,
              elapsedMs: Math.round(performance.now() - started),
              askedAt: new Date().toISOString(),
            });
            setPendingQuestion(null);
            setStreamText("");
            setStreamStage(null);
            setStreamRoute(null);
            setError(null);
            pushToast("Answer ready", "success");
          },
          onStreamError: (detail) => {
            setError({ message: detail, retriable: true });
            setStreamText("");
            setStreamStage(null);
          },
        },
      );
    } catch (err) {
      setError(describeError(err));
      setStreamText("");
      setStreamStage(null);
    } finally {
      setStreaming(false);
    }
  }

  function submit(question: string) {
    const trimmed = question.trim();
    if (!trimmed || streaming) return;
    lastSubmittedRef.current = trimmed;
    setPendingQuestion(trimmed);
    setError(null);
    setDraft("");
    void runStream(trimmed);
  }

  function handleRetry() {
    if (lastSubmittedRef.current && !streaming) {
      void runStream(lastSubmittedRef.current);
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
              streamText={streamText}
              streamStage={streamStage}
              streamRoute={streamRoute}
            />
          )}
          <div ref={bottomRef} aria-hidden="true" />
        </div>
        <div className={styles.composerArea}>
          <Composer
            value={draft}
            onChange={setDraft}
            onSubmit={() => submit(draft)}
            onStop={handleStop}
            submitting={streaming}
            includeSql={includeSql}
            onToggleSql={setIncludeSql}
          />
        </div>
      </div>

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
