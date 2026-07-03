import { useCallback, useState } from "react";
import { LOCALSTORAGE_KEYS } from "../lib/constants";
import { readJson, writeJson } from "../lib/persistence";

interface ConversationPayload {
  v: 1;
  id: string | null;
}

function load(): string | null {
  const data = readJson<ConversationPayload | null>(
    LOCALSTORAGE_KEYS.copilotConversation,
    null,
  );
  if (data && data.v === 1 && (typeof data.id === "string" || data.id === null)) {
    return data.id;
  }
  return null;
}

/**
 * Tracks the active server-side conversation id for the copilot.
 *
 * The backend mints an id on the first turn and returns it; we persist it so
 * follow-ups in the same browser session continue the same conversation.
 * "Start new topic" clears it so the next turn opens a fresh conversation.
 */
export function useConversation() {
  const [conversationId, setId] = useState<string | null>(load);

  const setConversationId = useCallback((id: string | null) => {
    setId((prev) => {
      if (prev === id) return prev;
      writeJson(LOCALSTORAGE_KEYS.copilotConversation, { v: 1, id });
      return id;
    });
  }, []);

  const clear = useCallback(() => {
    writeJson(LOCALSTORAGE_KEYS.copilotConversation, { v: 1, id: null });
    setId(null);
  }, []);

  return { conversationId, setConversationId, clear };
}
