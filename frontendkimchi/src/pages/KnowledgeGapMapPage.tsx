import { useState } from "react";
import { Info, Loader2, Map, RefreshCw } from "lucide-react";
import { useMutation } from "@tanstack/react-query";
import { apiClient, ApiError } from "../api/client";
import type { KnowledgeGapMap } from "../api/types";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import { ErrorState } from "../components/common/ErrorState";
import styles from "./KnowledgeGapMapPage.module.css";

/**
 * Calls the operator-only POST /knowledge-gap-map endpoint.
 * Returns the generated KnowledgeGapMap response.
 */
async function generateGapMap(): Promise<KnowledgeGapMap> {
  return apiClient.postJson<KnowledgeGapMap>("/knowledge-gap-map", {});
}

/** Maps coverage_quality values to human-readable labels and CSS class names. */
function qualityBadge(quality: string) {
  switch (quality) {
    case "poor":
      return { label: "Poor", className: styles.qualityPoor };
    case "fair":
      return { label: "Fair", className: styles.qualityFair };
    case "good":
      return { label: "Good", className: styles.qualityGood };
    default:
      return { label: quality, className: styles.qualityFair };
  }
}

export default function KnowledgeGapMapPage() {
  const [gapMap, setGapMap] = useState<KnowledgeGapMap | null>(null);
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: generateGapMap,
    onSuccess: (data) => {
      setGapMap(data);
      setError(null);
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        setError(err.detail);
      } else {
        setError("Failed to generate the knowledge gap map.");
      }
    },
  });

  const isInsufficient =
    gapMap !== null &&
    gapMap.eligible_outcome_count < gapMap.configured_minimum;

  return (
    <div className={styles.container}>
      <PageHeader
        title="Knowledge Gap Map"
        subtitle="Cluster low-quality query outcomes into topics and discover corpus improvement opportunities."
        actions={
          <button
            type="button"
            className="btn"
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            aria-label="Generate knowledge gap map"
          >
            {mutation.isPending ? (
              <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
            ) : (
              <RefreshCw size={14} aria-hidden="true" />
            )}
            {mutation.isPending ? "Generating…" : "Generate"}
          </button>
        }
      />

      {/* Error state */}
      {error && !mutation.isPending && (
        <ErrorState
          title="Knowledge gap map generation failed"
          body={error}
          action={
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => mutation.mutate()}
            >
              Retry
            </button>
          }
        />
      )}

      {/* Insufficient outcomes notice (R11.6) */}
      {isInsufficient && (
        <div className={styles.insufficientNotice} role="status" aria-live="polite">
          <Info size={20} aria-hidden="true" />
          <p className={styles.insufficientText}>
            The knowledge gap map requires at least{" "}
            <strong>{gapMap!.configured_minimum}</strong> eligible query outcomes
            (low-confidence, unanswered, or negatively rated) to produce
            meaningful clusters. Currently there are only{" "}
            <strong>{gapMap!.eligible_outcome_count}</strong>.
          </p>
        </div>
      )}

      {/* No data yet state */}
      {!gapMap && !error && !mutation.isPending && (
        <EmptyState
          icon={Map}
          title="No gap map generated yet"
          body='Click "Generate" to cluster low-quality outcomes into topics and get recommendations.'
        />
      )}

      {/* Topics display (R11.3) */}
      {gapMap && gapMap.topics.length > 0 && (
        <section className={styles.topicsSection} aria-label="Gap map topics">
          <h2 className={styles.sectionTitle}>Topics</h2>
          <div className={styles.topicGrid}>
            {gapMap.topics.map((topic) => {
              const badge = qualityBadge(topic.coverage_quality);
              return (
                <div key={topic.topic} className={styles.topicCard}>
                  <span className={styles.topicName}>{topic.topic}</span>
                  <div className={styles.topicMeta}>
                    <span
                      className={`${styles.qualityBadge} ${badge.className}`}
                      aria-label={`Coverage quality: ${badge.label}`}
                    >
                      {badge.label}
                    </span>
                    <span>
                      {topic.contributing_question_count}{" "}
                      {topic.contributing_question_count === 1
                        ? "question"
                        : "questions"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* Recommendations display */}
      {gapMap && !isInsufficient && hasRecommendations(gapMap) && (
        <section
          className={styles.recommendationsSection}
          aria-label="Recommendations"
        >
          <h2 className={styles.sectionTitle}>Recommendations</h2>

          {gapMap.recommended_missing_topics.length > 0 && (
            <div className={styles.recommendationCategory}>
              <h3 className={styles.categoryTitle}>Missing topics</h3>
              <ul className={styles.recommendationList}>
                {gapMap.recommended_missing_topics.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {gapMap.documents_needing_reingestion.length > 0 && (
            <div className={styles.recommendationCategory}>
              <h3 className={styles.categoryTitle}>Documents needing re-ingestion</h3>
              <ul className={styles.recommendationList}>
                {gapMap.documents_needing_reingestion.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {gapMap.suggested_benchmark_cases.length > 0 && (
            <div className={styles.recommendationCategory}>
              <h3 className={styles.categoryTitle}>Suggested benchmark cases</h3>
              <ul className={styles.recommendationList}>
                {gapMap.suggested_benchmark_cases.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}

          {gapMap.frequently_requested_topics.length > 0 && (
            <div className={styles.recommendationCategory}>
              <h3 className={styles.categoryTitle}>Frequently requested topics</h3>
              <ul className={styles.recommendationList}>
                {gapMap.frequently_requested_topics.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}
    </div>
  );
}

/** Returns true if the gap map has any non-empty recommendation category. */
function hasRecommendations(map: KnowledgeGapMap): boolean {
  return (
    map.recommended_missing_topics.length > 0 ||
    map.documents_needing_reingestion.length > 0 ||
    map.suggested_benchmark_cases.length > 0 ||
    map.frequently_requested_topics.length > 0
  );
}
