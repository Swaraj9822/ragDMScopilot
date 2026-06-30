// Clears user-scoped data that is cached in localStorage between sessions.
//
// Auth tokens are owned by the token store; this helper handles the *content*
// a signed-in user accumulates (Copilot history, document list, selection) so a
// logout/login leaves the next session with a clean slate and one user's data
// never leaks to another on a shared browser.
//
// UI preferences that are not tied to a specific user (theme, observability view
// options) are intentionally preserved.

import { LOCALSTORAGE_KEYS } from "./constants";
import { removeKey } from "./persistence";

const USER_SCOPED_KEYS: readonly string[] = [
  LOCALSTORAGE_KEYS.copilotHistory,
  LOCALSTORAGE_KEYS.documents,
  LOCALSTORAGE_KEYS.selectedDocuments,
];

/** Remove all user-scoped cached data. Safe to call when nothing is stored. */
export function clearUserData(): void {
  for (const key of USER_SCOPED_KEYS) removeKey(key);
}
