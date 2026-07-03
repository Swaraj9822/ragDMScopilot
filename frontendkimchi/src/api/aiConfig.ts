import { apiClient, TIMEOUT_SHORT_MS } from "./client";
import type { AIConfigurationVersion, ActivationEvent } from "./types";

// ---------------------------------------------------------------------------
// AI configuration history & management (R9)
// ---------------------------------------------------------------------------

export interface AIConfigHistoryResponse {
  config_id: string;
  active_version_id: string | null;
  versions: AIConfigurationVersion[];
  activation_events: ActivationEvent[];
}

export interface RollbackRequest {
  version_id: string;
  reason: string;
}

export function getAIConfigHistory(configId: string): Promise<AIConfigHistoryResponse> {
  return apiClient.get<AIConfigHistoryResponse>(
    `/ai-config/${encodeURIComponent(configId)}/history`,
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}

export function rollbackAIConfig(
  configId: string,
  payload: RollbackRequest,
): Promise<void> {
  return apiClient.postJson<void>(
    `/ai-config/${encodeURIComponent(configId)}/rollback`,
    payload,
  );
}

/** Approve a specific AI configuration version (operator-only, R8.3/R9). */
export function approveAIConfigVersion(
  configId: string,
  versionId: string,
): Promise<AIConfigurationVersion> {
  return apiClient.postJson<AIConfigurationVersion>(
    `/ai-config/${encodeURIComponent(configId)}/versions/${encodeURIComponent(versionId)}/approve`,
    {},
    { timeoutMs: TIMEOUT_SHORT_MS },
  );
}
