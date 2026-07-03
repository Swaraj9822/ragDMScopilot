import { useState } from "react";
import { Search, Lightbulb, AlertCircle } from "lucide-react";
import type { TraceDiagnosis, Recommendation } from "../../api/types";
import { diagnoseTrace } from "../../api/observability";
import { ApiError } from "../../api/client";
import styles from "./TraceInvestigator.module.css";

interface TraceInvestigatorProps {
  traceId: string;
}

/**
 * Provides a "Diagnose" action on a trace that calls the backend investigator
 * endpoint and displays the resulting cause + recommendations as read-only
 * suggestions. Never mutates configuration or corpus.
 *
 * Requirements: 10.6
 */
export function TraceInvestigator({ traceId }: TraceInvestigatorProps) {
  const [diagnosis, setDiagnosis] = useState<TraceDiagnosis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDiagnose = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await diagnoseTrace(traceId);
      setDiagnosis(result);
    } catch (err) {
      const message =
        err instanceof ApiError ? err.detail : "Failed to diagnose trace";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  const noCauseDetermined =
    diagnosis !== null &&
    diagnosis.recommendations.length === 0;

  return (
    <div className={styles.container}>
      <button
        type="button"
        className={`btn btn-primary ${styles.diagnoseBtn} ${loading ? styles.diagnosing : ""}`}
        onClick={handleDiagnose}
        disabled={loading}
        aria-label="Diagnose trace"
      >
        <Search size={14} aria-hidden="true" />
        {loading ? "Diagnosing…" : "Diagnose"}
      </button>

      {error && (
        <p className={styles.errorMessage} role="alert">
          <AlertCircle size={14} aria-hidden="true" /> {error}
        </p>
      )}

      {diagnosis && (
        <div className={styles.result} aria-label="Diagnosis result">
          {/* Cause section */}
          <div className={styles.causeSection}>
            <span className={styles.causeLabel}>Identified Cause</span>
            {noCauseDetermined ? (
              <p className={styles.noCause}>
                No cause determined — no specific issues were identified for this trace.
              </p>
            ) : (
              <p className={styles.causeDescription}>
                {diagnosis.cause_description}
              </p>
            )}
          </div>

          {/* Analyzed elements */}
          {diagnosis.analyzed_elements.length > 0 && (
            <div className={styles.analyzedElements} aria-label="Analyzed elements">
              {diagnosis.analyzed_elements.map((element) => (
                <span key={element} className={styles.elementBadge}>
                  {formatElement(element)}
                </span>
              ))}
            </div>
          )}

          {/* Recommendations (read-only suggestions) */}
          {!noCauseDetermined && diagnosis.recommendations.length > 0 && (
            <div className={styles.recommendationsSection}>
              <span className={styles.recommendationsLabel}>
                Recommendations
              </span>
              <span className={styles.readOnlyNotice}>
                These are suggestions only — no changes are applied automatically.
              </span>
              <ul className={styles.recommendationList}>
                {diagnosis.recommendations.map((rec, idx) => (
                  <RecommendationItem key={idx} recommendation={rec} />
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function formatElement(element: string): string {
  switch (element) {
    case "route":
      return "Route";
    case "retrieval_scores":
      return "Retrieval Scores";
    case "rerank_order":
      return "Rerank Order";
    case "generation_outcome":
      return "Generation Outcome";
    default:
      return element;
  }
}

interface RecommendationItemProps {
  recommendation: Recommendation;
}

function RecommendationItem({ recommendation }: RecommendationItemProps) {
  const targetClass =
    recommendation.target === "ai_configuration"
      ? styles.targetConfig
      : styles.targetCorpus;

  return (
    <li className={styles.recommendationItem}>
      <Lightbulb
        size={14}
        className={styles.recommendationIcon}
        aria-hidden="true"
      />
      <span className={styles.recommendationContent}>
        <span className={styles.recommendationDescription}>
          {recommendation.description}
        </span>
        <span className={`${styles.targetBadge} ${targetClass}`}>
          {recommendation.target === "ai_configuration" ? "Config" : "Corpus"}
        </span>
      </span>
    </li>
  );
}
