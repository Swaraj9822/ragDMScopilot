# Product

## Register

product

## Users
Internal operators and engineers working a `production-rag` backend. Three jobs:
asking grounded questions across documents and business data (Copilot), diagnosing
latency/errors/routing/retrieval/generation/ingestion through traces and logs
(AI Observability), and uploading/replacing source documents and following their
ingestion into Pinecone (Documents). Single-user internal tool; no auth, billing,
or notification surfaces.

## Product Purpose
A focused engineering instrument ("RAG Console") that makes a complex RAG +
observability backend legible: ask, verify the evidence/route/trace, inspect span
timing and correlated logs, and manage the retrieval corpus. Success = an operator
can answer, diagnose, and curate without leaving the three tabs, trusting that
every state shown reflects the real backend.

## Brand Personality
Exact, calm, diagnostic. "Ink and signal": near-black blue surfaces, restrained
status color, teal as the single active signal. Copilot reads conversational;
Observability reads precise; Documents makes an async pipeline obvious. Three words:
instrument, grounded, legible.

## Anti-references
Generic marketing/SaaS dashboards; gradient-heavy "AI product" aesthetics; glowing
neon; glassmorphism; oversized hero numbers; decorative charts; fabricated metrics
or fake progress. No data the backend does not actually return.

## Design Principles
- Honest states over decoration: loading, empty, partial, and failure are designed, never raw text.
- Show the evidence: route, trace, citations, SQL, and rows are always one glance from the answer.
- Restraint with one signal: teal marks the active/selected; amber/coral reserved for warning/failure; never color alone.
- Density that stays readable: dense data views, but body/identifier text holds AA contrast.
- The tool disappears into the task: standard affordances, consistent component vocabulary across the three tabs.

## Accessibility & Inclusion
WCAG 2.2 AA target. Keyboard-operable trace rows and spans, visible focus, skip
link, labelled inputs, polite aria-live for async outcomes, semantic equivalents
for the waterfall chart, `prefers-reduced-motion` honored, usable at 200% zoom.
