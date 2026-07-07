import { useEffect, useRef, useState } from "react";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { askStream, forgetConversation } from "../api/copilot";
import { ApiError, NetworkError, TimeoutError } from "../api/client";
import { useCopilotHistory } from "../hooks/useCopilotHistory";
import { useConversation } from "../hooks/useConversation";
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
  const { exchanges, append, clear: clearHistory } = useCopilotHistory();
  const { conversationId, setConversationId, clear: clearConversation } =
    useConversation();
  const { ids: selectedIds, remove: removeSelected, clear: clearSelected } =
    useSelectedDocuments();
  const { pushToast } = useToast();

  const [draft, setDraft] = useState("");
  const [includeSql, setIncludeSql] = useState(false);
  // The context panel (selected documents + conversation controls) is hidden by
  // default so the conversation gets the full width; it can be toggled open.
  const [showContext, setShowContext] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [error, setError] = useState<CopilotError | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  // Live streaming state for the in-flight answer.
  const [streaming, setStreaming] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [streamStage, setStreamStage] = useState<string | null>(null);
  const [streamRoute, setStreamRoute] = useState<string | null>(null);
  const [streamRewritten, setStreamRewritten] = useState<string | null>(null);

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
    setStreamRewritten(null);
    setError(null);

    try {
      await askStream(
        {
          question,
          documentIds: selectedIds,
          includeSql,
          conversationId,
          signal: controller.signal,
        },
        {
          onMeta: (meta) => {
            setStreamRoute(meta.route);
            // Adopt the server's conversation id as soon as it's known (minted
            // on the first turn) so the next follow-up continues this thread.
            if (meta.conversation_id) setConversationId(meta.conversation_id);
            if (meta.rewritten_question) setStreamRewritten(meta.rewritten_question);
          },
          onStatus: (stage) => setStreamStage(stage),
          onDelta: (text) => setStreamText((prev) => prev + text),
          onFinal: (response) => {
            if (response.conversation_id) setConversationId(response.conversation_id);
            append({
              id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
              question,
              rewrittenQuestion: response.rewritten_question,
              response,
              elapsedMs: Math.round(performance.now() - started),
              askedAt: new Date().toISOString(),
            });
            setPendingQuestion(null);
            setStreamText("");
            setStreamStage(null);
            setStreamRoute(null);
            setStreamRewritten(null);
            setError(null);
            pushToast("Answer ready", "success");
          },
          onClarification: (prompt) => {
            // Held-answer contract: the stream may end asking for one focused
            // clarification instead of answering. Surface the question so the
            // user can rephrase (rather than silently rendering nothing). Keep
            // the pending question visible, mirroring the error path.
            setStreamText("");
            setStreamStage(null);
            setStreamRoute(null);
            setError({
              message: `Clarification needed: ${prompt.clarification_question}`,
              retriable: true,
            });
          },
          onAbstention: (response) => {
            // Terminal abstention carries no answer content (R3.7); show the
            // missing-information notice instead of an empty answer.
            setStreamText("");
            setStreamStage(null);
            setStreamRoute(null);
            setError({
              message:
                response.missing_information ||
                "The assistant did not have enough evidence to answer this question.",
              retriable: true,
            });
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

  async function handleForgetContext() {
    if (!conversationId || streaming) return;
    try {
      await forgetConversation(conversationId);
      // Server has forgotten prior turns; clear the visible thread to match,
      // but keep the conversation id and the selected-document scope.
      clearHistory();
      setPendingQuestion(null);
      setStreamRewritten(null);
      setError(null);
      pushToast("Context cleared. Follow-ups will start fresh.", "success");
    } catch {
      pushToast("Couldn't clear context. Try again.", "error");
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
    <div className={`${styles.layout} ${showContext ? styles.withContext : ""}`}>
      <div className={styles.mainColumn}>
        <div className={styles.toolbar}>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setShowContext((v) => !v)}
            aria-pressed={showContext}
            aria-label={showContext ? "Hide context panel" : "Show context panel"}
          >
            {showContext ? (
              <PanelRightClose size={14} aria-hidden="true" />
            ) : (
              <PanelRightOpen size={14} aria-hidden="true" />
            )}
            {showContext ? "Hide panel" : "Context"}
          </button>
        </div>
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
              streamRewritten={streamRewritten}
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

      {showContext && (
        <ContextRail
          selectedIds={selectedIds}
          onRemove={removeSelected}
          historyCount={exchanges.length}
          conversationId={conversationId}
          onNewTopic={() => setConfirmClear(true)}
          onForgetContext={handleForgetContext}
          busy={streaming}
        />
      )}

      <ConfirmDialog
        open={confirmClear}
        title="Start a new topic?"
        body="This starts a fresh conversation and clears the visible history and selected documents in this browser. The previous conversation stays saved on the server."
        confirmLabel="Start over"
        onConfirm={() => {
          clearHistory();
          clearSelected();
          clearConversation();
          setPendingQuestion(null);
          setError(null);
          setStreamRewritten(null);
          setConfirmClear(false);
        }}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}
