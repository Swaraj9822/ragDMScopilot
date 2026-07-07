import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Database,
  Info,
  Lightbulb,
  Loader2,
  Search,
  Settings2,
} from "lucide-react";
import type { UnifiedQueryResponse } from "../../api/types";
import { diagnoseTrace, getTrace } from "../../api/observability";
import { ApiError } from "../../api/client";
import { CodeBlock } from "../common/CodeBlock";
import { CopyButton } from "../common/CopyButton";
import { RowsTable } from "./RowsTable";
import { formatDuration, shortenId } from "../../lib/format";
import { buildFindings, buildStages, type FindingTone } from "./buildInvestigation";
import styles from "./AnswerInvestigation.module.css";

interface AnswerInvestigationProps {
  response: UnifiedQueryResponse;
}

/**
 * The "AI Retrieval Investigator". A single control — "Why did the AI answer
 * this?" — that expands into a plain-language account of how the answer was
 * produced. It reuses the observability we already capture: the answer payload
 * supplies the deterministic findings (route, retrieval, SQL, rows, confidence,
 * claims), the correlated trace supplies the processing timeline, and the
 * `/traces/{id}/diagnose` endpoint supplies an AI root-cause narrative plus
 * concrete suggestions. Nothing is fetched until the panel is opened.
 */
export function AnswerInvestigation({ response }: AnswerInvestigationProps) {
  const [open, setOpen] = useState(false);
  const traceId = response.trace_id;

  const traceQuery = useQuery({
    queryKey: ["copilot-investigation-trace", traceId],
    queryFn: () => getTrace(traceId),
    enabled: open && !!traceId,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const diagnosisQuery = useQuery({
    queryKey: ["copilot-investigation-diagnosis", traceId],
    queryFn: () => diagnoseTrace(traceId),
    enabled: open && !!traceId,
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  const findings = useMemo(
    () => buildFindings(response, traceQuery.data),
    [response, traceQuery.data],
  );
  const stages = useMemo(
    () => (traceQuery.data ? buildStages(traceQuery.data) : []),
    [traceQuery.data],
  );

  return (
    <div className={styles.investigator}>
      <button
        type="button"
        className={styles.trigger}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls="answer-investigation-panel"
      >
        <Search size={15} aria-hidden="true" />
        Why did the AI answer this?
        {open ? (
          <ChevronDown size={15} aria-hidden="true" className={styles.triggerChevron} />
        ) : (
          <ChevronRight size={15} aria-hidden="true" className={styles.triggerChevron} />
        )}
      </button>

      {open && (
        <div id="answer-investigation-panel" className={styles.panel}>
          <h3 className={styles.panelHeading}>AI Investigation</h3>

          {/* ── Deterministic findings, straight from the answer payload ── */}
          <ul className={styles.findings}>
            {findings.map((f, i) => (
              <li key={i} className={styles.finding}>
                <FindingIcon tone={f.tone} />
                <span>{f.text}</span>
              </li>
            ))}
          </ul>

          {/* ── AI root-cause narrative + suggestions (from /diagnose) ── */}
          <section className={styles.diagnosis}>
            <h4 className={styles.subHeading}>
              <Lightbulb size={14} aria-hidden="true" />
              What the AI concluded
            </h4>
            {diagnosisQuery.isLoading ? (
              <p className={styles.loadingLine}>
                <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
                Investigating this answer…
              </p>
            ) : diagnosisQuery.isError ? (
              <p className={styles.diagnosisError}>
                <AlertTriangle size={14} aria-hidden="true" />
                {diagnosisQuery.error instanceof ApiError
                  ? diagnosisQuery.error.detail
                  : "The investigation could not be generated."}
              </p>
            ) : diagnosisQuery.data ? (
              <>
                <p className={styles.cause}>
                  {diagnosisQuery.data.cause_description || "No issues were detected."}
                </p>
                {diagnosisQuery.data.recommendations.length > 0 && (
                  <ul className={styles.suggestions}>
                    {diagnosisQuery.data.recommendations.map((rec, i) => (
                      <li key={i} className={styles.suggestion}>
                        {rec.target === "corpus" ? (
                          <Database size={14} aria-hidden="true" />
                        ) : (
                          <Settings2 size={14} aria-hidden="true" />
                        )}
                        <span>
                          <span className={styles.suggestionTarget}>
                            {rec.target === "corpus"
                              ? "Corpus"
                              : rec.target === "ai_configuration"
                                ? "AI configuration"
                                : rec.target}
                          </span>
                          {rec.description}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : null}
          </section>

          {/* ── The evidence, consolidated here instead of separate tabs ── */}
          {response.sql && (
            <Disclosure title="Generated SQL">
              <CodeBlock code={response.sql} title="SQL" />
            </Disclosure>
          )}

          {response.rows.length > 0 && (
            <Disclosure title={`Result rows (${response.rows.length})`}>
              <RowsTable rows={response.rows} />
            </Disclosure>
          )}

          {response.citations.length > 0 && (
            <Disclosure title={`Citations (${response.citations.length})`}>
              <div className={styles.citations}>
                {response.citations.map((c, i) => (
                  <div key={`${c.chunk_id}-${i}`} className={styles.citation}>
                    <div className={styles.citationHead}>
                      <span className={styles.citationNum}>{i + 1}</span>
                      <span className={styles.citationTitle}>
                        {c.title ?? "Untitled source"}
                      </span>
                    </div>
                    {(c.page_start != null || c.page_end != null) && (
                      <p className={styles.citationMeta}>
                        Pages {c.page_start ?? "?"}
                        {c.page_end != null && c.page_end !== c.page_start
                          ? `–${c.page_end}`
                          : ""}
                      </p>
                    )}
                    <div className={styles.citationIds}>
                      <span className="mono meta">doc {shortenId(c.document_id)}</span>
                      <CopyButton value={c.document_id} label="Copy document ID" iconOnly />
                      <span className="mono meta">chunk {shortenId(c.chunk_id)}</span>
                      <CopyButton value={c.chunk_id} label="Copy chunk ID" iconOnly />
                    </div>
                  </div>
                ))}
              </div>
            </Disclosure>
          )}

          {response.data_sources.length > 0 && (
            <Disclosure title={`Data sources (${response.data_sources.length})`}>
              <div className={styles.sources}>
                {response.data_sources.map((src) => (
                  <div key={src.table} className={styles.source}>
                    <span className="mono">{src.table}</span>
                    <span className="meta">{src.columns.join(", ")}</span>
                  </div>
                ))}
              </div>
            </Disclosure>
          )}

          {/* ── Processing timeline, straight from the correlated trace ── */}
          <Disclosure title="Processing timeline">
            {traceQuery.isLoading ? (
              <p className={styles.loadingLine}>
                <Loader2 size={14} className={styles.spinner} aria-hidden="true" />
                Loading trace…
              </p>
            ) : traceQuery.isError ? (
              <p className={styles.diagnosisError}>
                <AlertTriangle size={14} aria-hidden="true" />
                The processing timeline could not be loaded.
              </p>
            ) : stages.length > 0 ? (
              <ol className={styles.timeline}>
                {stages.map((stage, i) => (
                  <li key={i} className={styles.stage}>
                    {stage.status === "error" ? (
                      <AlertTriangle size={13} className={styles.stageError} aria-hidden="true" />
                    ) : (
                      <Check size={13} className={styles.stageOk} aria-hidden="true" />
                    )}
                    <span className={styles.stageOp}>{stage.operation}</span>
                    <span className={styles.stageDuration}>
                      {formatDuration(stage.durationMs)}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="meta">No processing steps were recorded.</p>
            )}
          </Disclosure>
        </div>
      )}
    </div>
  );
}

function FindingIcon({ tone }: { tone: FindingTone }) {
  if (tone === "warn") {
    return <AlertTriangle size={15} className={styles.iconWarn} aria-hidden="true" />;
  }
  if (tone === "info") {
    return <Info size={15} className={styles.iconInfo} aria-hidden="true" />;
  }
  return <Check size={15} className={styles.iconOk} aria-hidden="true" />;
}

function Disclosure({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className={styles.disclosure}>
      <button
        type="button"
        className={styles.disclosureToggle}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {title}
      </button>
      {open && <div className={styles.disclosureBody}>{children}</div>}
    </section>
  );
}
