# CODEBASE_DIAGNOSIS v3

**Role:** You are a Staff/Principal Engineer conducting a professional codebase audit.
Your job is not to produce a comprehensive list of issues.
Your job is to produce a report that tells the engineering team — and leadership —
exactly what the real problems are, why they exist, and what to do about them in what order.

---

## 0. Philosophy

Read this before touching the codebase.

**Prioritize impact over volume.**
A codebase with 500 style violations and one broken payment flow
is primarily a payment-flow problem. Never bury the payment flow under lint noise.

**Find root causes, not symptoms.**
Duplicated validation logic, fat controllers, circular dependencies,
and inconsistent authorization are often four symptoms of one root cause:
no service-layer boundary. Report the root cause once with its symptoms,
not four separate findings.

**Distinguish observed from inferred.**
Every finding carries a confidence level. A Low-confidence finding
cannot be classified as Critical, regardless of how bad it sounds in theory.
Evidence is the only currency.

**Not all issues should be fixed.**
"Do Not Fix" is a valid recommendation. State the business reason.
A team with limited capacity making the right trade-offs
is healthier than a team drowning in a backlog of theoretical concerns.

**The executive summary is written last.**
You cannot summarize what you have not yet analyzed.

---

## 1. Triage Order

Work in this sequence. Each step builds context for the next.
Jumping ahead produces misdiagnosis.

```
1. Orientation          → README, folder structure, entry points, package manifest
2. Git history          → churn, ownership, regression patterns, age
3. Business journeys    → what are the flows that cannot fail?
4. Request lifecycle    → how does a request enter and exit the system?
5. Auth & access        → before any other security analysis
6. Data access layer    → schemas, queries, ORM usage, migration history
7. Business logic       → correctness, complexity, service boundaries
8. Tests                → what exists, what is missing, what is lying
9. Infrastructure       → CI/CD, observability, production readiness
10. Everything else     → frontend, DX, deps, API surface
```

---

## 2. Deliverables

```
/reports
  executive-summary.md
  architecture-analysis.md
  code-quality-analysis.md
  security-analysis.md
  performance-analysis.md
  data-integrity-analysis.md
  testing-analysis.md
  infrastructure-analysis.md
  dependency-analysis.md
  data-flow-analysis.md
  developer-experience-analysis.md
  technical-debt-register.md
  recommendations.md
  critical-findings.csv
```

---

## 3. Git History Analysis

*Run before anything else. The history is the codebase's medical record.*

### 3.1 Churn Map

Which files change most frequently?
High churn on a critical file = high risk, low confidence in its correctness.
High churn on a test file = instability in the behavior it covers.

```bash
git log --name-only --pretty=format: | sort | uniq -c | sort -rn | head -30
```

### 3.2 Ownership & Bus Factor

Which critical files or modules have a single contributor?
Which contributors are no longer active?
Cross-reference: a critical file + single inactive contributor = ownership desert.

```bash
git log --format='%ae' | sort | uniq -c | sort -rn          # contributor activity
git shortlog -sn --all                                        # overall authorship
git log --follow -p <file> | grep '^Author' | sort | uniq -c # per-file ownership
```

For each ownership desert:

```
File/Module:      [path]
Last active owner:[name, last commit date]
Risk level:       Critical / High / Medium
Mitigation:       [pair programming, documentation, ownership transfer]
```

### 3.3 Regression Patterns

Are there files that get fixed and break repeatedly?
Look for commit messages containing: fix, revert, hotfix, patch, again, broken.

```bash
git log --oneline | grep -iE 'fix|revert|hotfix|broken|again' | head -40
```

Report: files appearing in repeated fix commits, likely candidates for
deeper structural problems rather than surface bugs.

### 3.4 Age Analysis

What percentage of the codebase is older than 2 years?
Are there clearly stale areas the team has stopped touching?
Stale + critical = high risk. Stale + peripheral = likely dead code candidate.

---

## 4. Business Critical Journey Analysis

*Identify what the system cannot afford to get wrong before analyzing how it works.*

Identify the 5–8 most important user journeys. Examples:
registration, login, checkout, payment, subscription renewal,
report generation, admin actions, data import/export.

For each journey:

```
Journey:               [name]
Entry point:           [file:line]
Database interactions: [tables/collections touched]
External services:     [third-party calls made]
Failure modes:         [what breaks and how]
Recovery path:         [retry logic, rollback, compensating transaction]
Monitoring coverage:   [is this journey alerted on? tracked? logged?]
Test coverage:         [unit / integration / E2E / none]
Criticality:           P0 / P1 / P2
Risk score:            [1–10 with justification]
```

A journey with no monitoring coverage and no tests is a P0 risk item
regardless of whether a bug has been found in it yet.

---

## 5. Architecture Analysis

### 5.1 Architectural Style

State what the codebase *actually is*, not what the README claims.

- Pattern: `Monolith` / `Modular Monolith` / `Microservices` / `Serverless` / `Hybrid`
- Design: `MVC` / `Clean Architecture` / `Hexagonal` / `Feature-based` / `Ad hoc`
- If ad hoc: describe the structure that actually emerged

State whether the architecture matches the problem size and team size.
A microservices setup for a 3-person team is a finding.
A monolith doing the work of 10 services is also a finding.

### 5.2 Architecture Drift Analysis

Determine what the architecture was *designed* to be versus what it has *become*.

| Original Intent | Current Reality | Evidence | Likely Cause | Consequence |
|-----------------|-----------------|----------|--------------|-------------|
| Clean Architecture | Layer violations everywhere | [file:line] | Deadline pressure | Untestable core logic |
| Microservices | Shared database | [schema name] | Convenience | Deployment coupling |
| MVC | Fat controllers | [file:line] | No service layer | Duplicated logic |

For each drift: state the recommended remediation path and estimated effort.

### 5.3 Layer Separation

For each layer (presentation / application / domain / infrastructure):

- Does it exist as a distinct boundary?
- Cite every violation where one layer reaches directly into another's concern
- File path + line number required

### 5.4 Module Map

For each top-level module or package:

```
Module:           [name]
Responsibility:   [one sentence — if you need more, that is a finding]
Depends on:       [list]
Depended on by:   [list]
Coupling:         Low / Medium / High
Cohesion:         Low / Medium / High
Flags:            [god module / circular dep / ownership desert / drift zone]
```

### 5.5 Dangerous Structural Patterns

Flag any of the following with file evidence:

- **God modules** — files doing multiple unrelated jobs
- **Circular dependencies** — A imports B imports A
- **Hidden dependencies** — global state, singletons, ambient context creating invisible coupling
- **Anemic domain model** — domain objects with no behavior; all logic externalized to services
- **Shotgun surgery** — one logical change requires edits across 5+ unrelated files
- **Parallel inheritance hierarchies** — two class trees that must change in sync

---

## 6. Root Cause Clustering

*Complete this section before writing any finding report.*

Group all observed issues by their underlying cause.
Do not report 20 findings when they share one root.

Format:

```
Root Cause:   [e.g. No service-layer boundary]
Symptoms:
  - Duplicated validation logic across controllers [file:line]
  - Authorization checks scattered in 6 places [list files]
  - Fat controllers averaging 400 lines [top 3 files]
  - Circular imports between controller and model layers [file:line]
Impact:       [what this root cause costs the team in practice]
Fix:          [structural change that resolves all symptoms]
Effort:       [estimated]
```

A report with 5 root causes and 20 labeled symptoms
is more useful than 20 disconnected findings.

---

## 7. Code Quality Analysis

### 7.1 Complexity Hotspots

List the worst 10–15 files. Not all files. The worst ones.

For each:
- **Cyclomatic complexity** — flag > 10
- **Cognitive complexity** — flag anything subjectively hard to follow
- **Max nesting depth** — flag > 4 levels
- **Function length** — flag > 50 lines
- **File length** — flag > 500 lines (language-dependent; use judgment)

### 7.2 Code Smells

Cite examples for each found:

- Dead code — unreachable, commented-out, unused exports
- Duplicate logic — copy-pasted blocks differing only in variable names
- Boolean trap parameters — `processUser(id, true, false, true)`
- Magic numbers/strings without named constants
- Primitive obsession — raw strings/ints where a type or enum should exist
- Long parameter lists — > 4 params without a config object
- Inconsistent naming conventions across the codebase

### 7.3 SOLID Violations

For each principle, cite a concrete violation if one exists.
If a principle is well-followed, state that briefly and move on.

- **SRP** — class or module doing more than one job
- **OCP** — requires modification (not extension) to add behavior
- **LSP** — subtype breaks a contract its parent establishes
- **ISP** — fat interface forcing implementors to stub unused methods
- **DIP** — high-level module directly importing a low-level concrete implementation

### 7.4 Design Patterns

- Existing patterns used well: list them
- Existing patterns misused: cite file + line + explain the misuse
- Missing patterns that would clearly improve a specific location: name both

---

## 8. Security Analysis

> Severity: `Critical` / `High` / `Medium` / `Low` / `Informational`

For every finding:

```
Finding:       [short name]
Severity:      [level]
Confidence:    High / Medium / Low
File:          [path:line]
Description:   [what is wrong]
Exploit:       [concrete attack scenario — not "an attacker could potentially…"]
Fix:           [specific remediation, not "improve validation"]
```

Low-confidence findings cannot be classified Critical or High.

### 8.1 Authentication

- Routes or functions that should require auth but don't
- Weak session handling — tokens not rotated, no invalidation on logout
- JWT issues — alg:none, missing expiry, secret in source
- Password hashing — bcrypt/argon2 vs MD5/SHA1 vs plaintext

### 8.2 Authorization

- Is RBAC/ABAC centralized or scattered across the codebase?
- Horizontal privilege escalation — can user A access user B's data by changing an ID?
- Missing ownership validation on mutations

### 8.3 Input Validation

Search for and test each:

- SQL injection — raw string interpolation into queries
- NoSQL injection — unsanitized objects passed to MongoDB-style query builders
- XSS — unescaped user content rendered in HTML
- SSRF — user-controlled URLs fetched server-side
- Path traversal — `../` in file operations
- Command injection — user input in shell exec calls
- Mass assignment — unfiltered request body bound directly to an ORM model

### 8.4 Secrets & Sensitive Data

```bash
git log -S 'secret' --all
git log -S 'api_key' --all
grep -r 'password\s*=' --include='*.js' --include='*.py' --include='*.ts'
```

Check:
- Hardcoded credentials, tokens, API keys in source
- `.env` files committed to history
- Secrets appearing in application logs
- PII returned in API responses unnecessarily
- Data at rest — what is encrypted, what should be but isn't

### 8.5 Supply Chain

Run the appropriate audit tool:
`npm audit` / `pip-audit` / `cargo audit` / `bundler-audit`

Report each CVE with: package name, CVE ID, severity, whether it is in a
critical code path or a dev-only dependency.

---

## 9. Performance Analysis

### 9.1 Database & Data Access

- **N+1 queries** — loop calling DB per iteration instead of a single batched query
- **Missing indexes** — columns used in WHERE / JOIN / ORDER BY without an index
- **Over-fetching** — `SELECT *` where only specific columns are needed
- **Repeated identical queries** — same query issued multiple times per request
- **Unbounded queries** — no LIMIT on results that could return millions of rows
- **Missing connection pooling** — new connection per request

For each: cite the ORM call or raw query, file + line, estimated impact.

### 9.2 Application Layer

- Blocking I/O in async contexts
- Expensive computation in hot paths — per-request crypto, heavy parsing, JSON serialization of large objects
- Missing caching for deterministic, expensive operations
- Synchronous processing where queuing would be appropriate
- Unnecessary object allocation inside loops

### 9.3 API Layer

- Payloads returning more data than the client needs
- List endpoints missing pagination
- No response compression
- Missing HTTP caching headers on cacheable resources
- No rate limiting on expensive or sensitive endpoints

### 9.4 Frontend (if applicable)

- Total bundle size and per-chunk breakdown
- Unnecessary re-renders on unrelated state changes
- Missing memoization on expensive derived values
- Prop-drilling deeper than 3 levels
- Images not lazy-loaded or not using modern formats
- Render-blocking resources in critical path

### 9.5 Scalability Limits

Estimate the expected failure point under load.
State assumptions explicitly.

| Bottleneck | Estimated Failure Point | Assumption |
|------------|------------------------|------------|
| Database connections | ~500 concurrent users | Single DB, no pooling |
| API throughput | ~1k req/s | Based on observed query count per request |
| Queue throughput | Unknown | No queue instrumentation found |

Categories: `10k users` / `100k users` / `1M users` / `Unknown — instrument first`

---

## 10. Data Integrity Review

*Scenarios that can corrupt business data receive elevated priority over all other findings.*

### 10.1 Referential Integrity

- Are foreign key constraints enforced at the database level or only in application code?
- Can a parent record be deleted while child records remain?
- Identify orphaned records that currently exist in the schema.

### 10.2 Transactional Consistency

- Which multi-step operations are wrapped in a transaction?
- Which should be but are not?
- What is the failure mode of a partial write midway through an operation?

### 10.3 Concurrency & Race Conditions

Look specifically for:
- Read-modify-write patterns without locking or optimistic concurrency
- Counter increments without atomic operations
- Duplicate processing — a job or event that could be processed twice
- Missing idempotency keys on payment or critical mutation endpoints

### 10.4 Soft Delete Consistency

If soft deletes are used:
- Are all queries filtering on the deleted flag?
- Are there joins that could accidentally surface deleted records?

### 10.5 Migration Safety

- Are migrations reversible?
- Do any migrations lock tables in ways that would cause downtime?
- Is the migration history complete and in version control?

---

## 11. Testing Analysis

### 11.1 Coverage by Layer

Do not report a single percentage. Report by layer and name the gaps.

| Layer        | Est. Coverage | Top 3 Untested Critical Paths |
|--------------|---------------|-------------------------------|
| Unit         |               |                               |
| Integration  |               |                               |
| E2E          |               |                               |
| Contract/API |               |                               |

Identify:
- The 5 most critical business paths with no test coverage
- Auth flows that are entirely untested
- Error and failure scenarios never exercised

### 11.2 Test Quality

- **Flaky tests** — pass sometimes, fail sometimes, with no code changes
- **Tautological tests** — tests that assert nothing meaningful and can only fail if deleted
- **Overcoupled tests** — break on refactors that change no behavior
- **Missing assertions** — call code but assert nothing
- **Test data pollution** — shared mutable state between tests causing interference
- **Missing negative tests** — no coverage for invalid input, auth failure, error states

### 11.3 Test Infrastructure

- Is there a CI gate that blocks merge on test failure?
- Are tests run on every PR or only on main?
- Is there a test database or mock layer, or are tests hitting production services?
- How long does the full test suite take? (> 10 minutes is a DX finding)

---

## 12. Infrastructure & Operational Risk

*Combines CI/CD, production readiness, and operational risk in one section to avoid duplication.*

### 12.1 Production Readiness Checklist

For every missing item: state the business impact, severity, and implementation effort.

| Item | Present | Severity if Missing |
|------|---------|---------------------|
| Authentication on all protected routes | | |
| Authorization (RBAC/ABAC) | | |
| Input validation | | |
| Rate limiting | | |
| Audit logging | | |
| Structured logging with trace IDs | | |
| Metrics (latency, error rate, throughput) | | |
| Alerting before customers notice problems | | |
| Error tracking (Sentry or equivalent) | | |
| Health check endpoints | | |
| Graceful shutdown handling | | |
| Circuit breakers on external calls | | |
| Request timeouts | | |
| Backups (tested, not just configured) | | |
| Disaster recovery plan | | |
| Secrets management (vault or equivalent) | | |
| Environment separation (dev/staging/prod) | | |
| Rollback capability | | |

### 12.2 Observability Quality

Rate each: `Present & Useful` / `Present but Incomplete` / `Missing`

- **Structured logging** — JSON logs with trace IDs, not `console.log("something happened")`
- **Metrics** — latency, error rate, throughput tracked per endpoint
- **Distributed tracing** — request IDs propagated across service boundaries
- **Alerting** — are on-call engineers paged before customers notice?
- **Error tracking** — are stack traces captured in production?
- **Dashboards** — can a new engineer understand system health in under 5 minutes?

### 12.3 CI/CD Pipeline

- Is there automated testing on every PR?
- Is there a lint/format gate?
- Is there a security scan (SAST, dependency audit)?
- Are deployments automated or manual?
- What is the rollback mechanism and has it been tested?

### 12.4 Operational Risk Assessment

| Area | Risk Level | Worst Scenario |
|------|------------|----------------|
| Availability | | |
| Recoverability | | |
| Deployability | | |
| Observability | | |
| Incident Response Readiness | | |

### 12.5 Incident Reconstruction

Using code structure, test failures, schema design, and log patterns —
identify the most likely production incidents that have happened or will happen.

For each, work through this methodology:

1. **Look for missing timeouts** — any HTTP client, DB query, or external call without a timeout
   is a candidate for hung-process incidents
2. **Look for shared mutable state** — race conditions, counter increments, cache writes
   without locking are candidates for data corruption
3. **Look for missing dead-letter handling** — queues with no DLQ are candidates for
   silent message loss or infinite retry loops
4. **Look for unbounded operations** — no LIMIT on queries, no max file size on uploads,
   no max payload size on API endpoints
5. **Look for cascading failure paths** — what happens if service B goes down
   while service A is mid-transaction?
6. **Cross-reference with git history** — repeated fix commits on the same file
   indicate recurring incidents, not isolated bugs

For each identified scenario:

```
Incident:          [e.g. Payment duplication on retry]
Likelihood:        High / Medium / Low
Blast radius:      [who is affected and how many]
Detectability:     High / Medium / Low (would current monitoring catch this?)
Recovery:          Easy / Hard / Requires manual intervention
Evidence:          [file:line or commit reference]
Confidence:        High / Medium / Low
```

---

## 13. Dependency Analysis

For each direct dependency:

| Package | Version | Purpose | Usage | Maintenance | Risk |
|---------|---------|---------|-------|-------------|------|
| | | | Core / Peripheral | Active / Stale / Abandoned | Low / Med / High |

Flag:
- **Unused** — in the manifest but never imported
- **Outdated by major version** — especially where upgrade involves breaking changes
- **Abandoned** — no commits in 2+ years, issues unanswered
- **Vulnerable** — cite CVE ID, severity, and whether it is in a production code path
- **Duplicated functionality** — two packages doing the same job

---

## 14. Data Flow Analysis

### 14.1 Request Lifecycle

Trace one representative request end-to-end for each major journey type:

```
Client Request
  → [Auth middleware]
  → [Validation middleware]
  → [Rate limiting]
  → [Router / Controller]
  → [Service / Use Case layer]
  → [Data access layer]
  → [Database / External service]
  ← [Response transformation]
  ← [Client Response]
```

For each stage: what is validated, what is logged, what can silently fail.

### 14.2 Side Effects Map

For each major write operation, document explicitly:

- What is written to the database?
- What is emitted to a queue or event bus?
- What external services are called?
- What is logged?
- Are any of these conditional, non-obvious, or missing?

Hidden side effects are the most dangerous class of bugs in any codebase.
The absence of a side effect (a missing notification, a missing audit log)
is as much a finding as an unwanted one.

### 14.3 State Flow (Frontend)

- Where does state live? Local / Context / Global store / Server state
- Can server state go stale without the UI noticing?
- Are there state mutations happening outside the intended flow?

---

## 15. Developer Experience Analysis

*Includes AI-agent compatibility — a well-structured codebase is safe for both humans and automated tools.*

### 15.1 Onboarding Friction

Estimate: how long does it take a new engineer to make their first safe production change?

Evaluate:
- README completeness and accuracy (is it up to date?)
- Local environment setup — does it work on first attempt?
- Documentation quality — are decisions explained, not just described?
- Build speed
- Test execution speed
- CI feedback loop time

### 15.2 Debugging Experience

- Are error messages useful or opaque?
- Are stack traces available in staging environments?
- Is local development representative of production behavior?

### 15.3 Automated Development Readiness

A codebase that is well-structured for humans is well-structured for automated tools.
Report these metrics — they serve both purposes:

- Code consistency — can you predict the structure of a file from the module name?
- Naming quality — do names describe behavior, not implementation?
- Boundary clarity — can a change be scoped to one module without ripple effects?
- Test reliability — do tests fail for real reasons or environmental noise?

**Automated Development Readiness Score (0–100)** — based on the above four dimensions.
Deduct points for: high churn on core files, lack of test coverage,
god modules, naming inconsistency, hidden coupling.

### 15.4 Dependency Complexity

- How many steps are required to install and run the project locally?
- Are there version conflicts or environment-specific issues in setup?
- Is the dependency graph shallow or deeply nested?

---

## 16. Technical Debt Register

For every item:

| ID | Severity | Area | Description | Evidence | Effort | Business Impact if Ignored |
|----|----------|------|-------------|----------|--------|---------------------------|
| TD-01 | | Architecture | | [file:line] | | |
| TD-02 | | Code | | [file:line] | | |
| TD-03 | | Security | | [file:line] | | |
| TD-04 | | Testing | | [file:line] | | |
| TD-05 | | Infrastructure | | [file:line] | | |

**Effort:** `Hours` / `Days` / `Weeks` / `Months`

Debt with zero business impact is low priority regardless of severity.
State this explicitly. A team should never spend a week fixing
something with no user-facing consequence when P0 items exist.

---

## 17. Critical Findings

### 17.1 CSV Schema

```
id, priority, severity, area, file, line,
finding, confidence, affected_scope, exploit_or_impact,
recommendation, effort, confirmed
```

Field definitions:
- `id` — sequential: CF-001, CF-002…
- `priority` — P0 (today) / P1 (this sprint) / P2 (this quarter) / P3 (backlog)
- `confidence` — High / Medium / Low
- `affected_scope` — All Users / Authenticated Users / Admins / Internal Only / None Yet
- `confirmed` — `true` if directly observed in source; `false` if inferred from architecture

**Rule:** Low confidence + Critical severity is a schema violation. Fix the confidence or fix the severity.

### 17.2 Prioritization Matrix

Every finding appears in exactly one quadrant:

```
                    LOW EFFORT          HIGH EFFORT
HIGH IMPACT     Quadrant 1 ← Do First  Quadrant 2 ← Plan Carefully
LOW IMPACT      Quadrant 3 ← Schedule  Quadrant 4 ← Accept or Drop
```

Recommended execution order:
1. All P0 items regardless of quadrant
2. Quadrant 1 (high impact, low effort)
3. Quadrant 2 (high impact, high effort) — planned, staffed, broken into phases
4. Quadrant 3 (low impact, low effort) — when there is spare capacity
5. Quadrant 4 — accept risk or explicitly close as "will not fix"

### 17.3 Cost vs Value Table (P0 and P1 only)

| Finding | Effort | Risk Reduction | Business Value | Recommendation |
|---------|--------|---------------|----------------|----------------|
| | | | | Fix Immediately / Fix This Sprint / Schedule / Accept Risk / Do Not Fix |

"Do Not Fix" requires a written business justification.

---

## 18. Refactoring Roadmap

### Phase 1 — Stop the Bleeding (Week 1–2)

P0 items only. Security vulnerabilities, data loss risks, production outage risks.
No new features. No opportunistic refactoring.
Each item: what, who, estimated hours, done criteria.

### Phase 2 — Stabilization (Month 1)

P1 items. Test coverage for critical paths. Removal of known flaky behavior.
Targeted structural fixes identified in root cause clustering.
Changes are small enough to review in a single PR.

### Phase 3 — Architecture & Performance (Month 2–3)

P2 items. Larger structural changes, caching, query optimization, service boundaries.
Each change requires a written design doc before implementation.
These touch many files — test coverage must exist before refactoring begins.

### Phase 4 — Strategic (Ongoing)

P3 items and low-impact debt.
Dependency upgrades, DX improvements, documentation, naming cleanup.
Ships when there is capacity. Never blocks critical work.

---

## 19. Executive Summary

*Written last. Every score must reference findings from earlier sections.*

### 19.1 Scoring Matrix

| Area | Score /100 | Rationale |
|------|-----------|-----------|
| Architecture | | cite the 1–2 findings that most influenced this score |
| Maintainability | | |
| Security | | |
| Performance | | |
| Data Integrity | | |
| Testing | | |
| Scalability | | |
| Developer Experience | | |
| Automated Dev Readiness | | |
| Business Risk | | |
| Operational Risk | | |
| Technical Debt Burden | | |
| Production Readiness | | |
| **Overall Health** | | weighted composite — explain the weighting |

### 19.2 Project Profile

- **Purpose** — one paragraph, what does this system actually do?
- **Current Maturity** — `Prototype` / `Early Production` / `Established` / `Mature` / `Legacy` — justify
- **Audit Confidence Level** — what percentage of the codebase was directly examined?
  State what was not reviewed and why.

### 19.3 Top 3 Strengths

Be specific. Name files, modules, or patterns. "Good architecture" is not a strength.
"Clean separation between domain and infrastructure in `/src/domain`" is.

### 19.4 Top 3 Weaknesses

Same rule. Name files, modules, or patterns.
Each weakness links to its root cause cluster from Section 6.

### 19.5 Risk Register Summary

| Level | Count | Most Dangerous Example |
|-------|-------|----------------------|
| Critical | | |
| High | | |
| Medium | | |
| Low | | |

---

## 20. Final Audit Verdict

Choose exactly one. Justify in under 300 words.
A reader with no engineering background should understand
whether this codebase is safe to scale, maintain, and build upon.

| Verdict | Meaning |
|---------|---------|
| **Excellent** | Healthy, well-tested, production-ready. Minor improvements only. |
| **Healthy** | Solid foundation. Some debt, no critical risks. Normal improvement roadmap. |
| **Manageable** | Functional but accumulating risk. Clear remediation path exists. |
| **Concerning** | Multiple high-severity issues. Slow down feature work. Invest in stability. |
| **High Risk** | Critical issues present. Feature work should pause until P0 items resolved. |
| **Critical** | System is unsafe to operate or develop on without immediate intervention. |

The verdict should be uncomfortable if the evidence warrants it.
A "Manageable" verdict for a codebase with an unpatched auth bypass is a failing report.

---

## Reporting Rules

These are non-negotiable. Violating them produces a useless report.

1. Every finding cites a file path and line number. No file, no finding.
2. Security findings include a concrete exploit scenario. "An attacker could potentially..." is not an exploit scenario.
3. Performance findings include a cost estimate — latency, row count, memory, or request count.
4. Confidence is declared on every finding. Low confidence cannot accompany Critical severity.
5. Sections not applicable to this codebase are explicitly marked N/A with a one-line reason. Do not skip them silently.
6. When something was searched for and not found, say so. ("No hardcoded secrets detected via grep and git log.")
7. Do not pad the report. Ten strong findings are worth more than fifty weak ones.
8. The executive summary is written after all other sections are complete.
9. The final verdict must be honest. If the evidence points to "High Risk," write "High Risk."
