# Implementation Recommendations — Prioritized Action Plan

**Prepared for:** ragDMScopilot main branch (86e1137)  
**Review Date:** July 5, 2026  
**Total Effort Estimate:** 12–15 hours (implementation + testing + review)

---

## 🎯 Priority Matrix

```
                High Impact
                    ↑
        ┌───────────────────────┐
  HIGH  │  1. Few-shot SQL      │ 2. Tunable top-K
 EFFORT │     generation        │
        │  3. Cleanup tokens    │
        ├───────────────────────┤
  LOW   │  4. Timeout metrics   │ 5. Reranker warning
 EFFORT │  6. Stream tests      │ 7. GC chunks
        └───────────────────────┘
        Low Impact          High Impact →
```

**Recommendation:** Do 1–3 first (enables scale); then 4–7 (polish).

---

## 1️⃣ **PRIORITY 1: Few-Shot SQL Generation** ⭐⭐⭐

**Current State:** SQL generation is cold-start (schema dump → LLM → SQL). No examples shown.

**Impact:** 
- ↑ 15–25% reduction in invalid SQL rejections
- ↓ 1–2 queries/user needed before hitting valid SQL
- ↑ Generation time ~+200ms (semantic retrieval overhead)

**Effort:** 3 hours | **Risk:** Low (adds context, no logic change)

### Implementation

#### Step 1: Add similarity function
```python
# src/rag_system/copilot.py

from sklearn.metrics.pairwise import cosine_similarity  # or use embeddings

def _semantic_similarity(text1: str, text2: str) -> float:
    """Simple keyword overlap; upgrade to embeddings if needed."""
    words1 = set(text1.lower().split())
    words2 = set(text2.lower().split())
    if not words1 or not words2:
        return 0.0
    overlap = len(words1 & words2)
    return overlap / max(len(words1), len(words2))
```

#### Step 2: Modify prompt builder
```python
def build_sql_prompt_with_examples(
    question: str, 
    catalog_description: str,
    examples: list[tuple[str, str]] = None  # [(question, sql), ...]
) -> str:
    examples_block = ""
    if examples:
        examples_block = "Examples of valid queries:\n\n"
        for ex_q, ex_sql in examples[:3]:  # top 3
            examples_block += f"Question: {ex_q}\nSQL:\n{ex_sql}\n\n"
    
    return dedent(f"""
        You generate PostgreSQL SELECT queries for an enterprise data copilot.
        {examples_block}
        Use only the approved schema below. Generate exactly one read-only SELECT query.
        ... (rest of prompt)
        
        Approved schema:
        {catalog_description}
        
        User question:
        {question}
    """).strip()
```

#### Step 3: Update query() in DatabaseCopilotService
```python
def query(self, request: CopilotQueryRequest) -> CopilotQueryResponse:
    # ... existing setup ...
    
    # NEW: Fetch similar cases from evaluation set
    examples = self._get_similar_sql_examples(request.question, top_k=3)
    
    with timed(logger, "copilot SQL generation", **log_extra):
        sql = self.generator.generate_sql(
            request.question, 
            self.catalog,
            examples=examples
        )
    
    # ... rest of method unchanged ...

def _get_similar_sql_examples(
    self, question: str, top_k: int = 3
) -> list[tuple[str, str]]:
    """Retrieve top-k similar SQL examples from the eval set."""
    if not hasattr(self, '_cached_benchmark_cases'):
        from rag_system.models import BenchmarkCase
        self._cached_benchmark_cases = [
            (c.question, c.sql) 
            for c in self.list_benchmark_cases()
            if c.sql  # only cases with golden SQL
        ]
    
    if not self._cached_benchmark_cases:
        return []
    
    similarities = [
        (i, _semantic_similarity(question, q))
        for i, (q, _) in enumerate(self._cached_benchmark_cases)
    ]
    similarities.sort(key=lambda x: x[1], reverse=True)
    
    return [
        self._cached_benchmark_cases[i] 
        for i, _ in similarities[:top_k]
    ]
```

#### Step 4: Update prompt builder signature
```python
# In BedrockDatabaseCopilot.generate_sql():

def generate_sql(
    self, 
    question: str, 
    catalog: CopilotSchemaCatalog,
    examples: list[tuple[str, str]] = None
) -> str:
    prompt = build_sql_prompt_with_examples(
        question, 
        catalog.describe_for_prompt(),
        examples
    )
    return _extract_sql(self._call_llm(prompt))
```

#### Step 5: Write test
```python
# tests/test_copilot_few_shot.py

def test_copilot_few_shot_improves_generation():
    """Verify few-shot examples are included in the prompt."""
    service = DatabaseCopilotService(settings)
    examples = service._get_similar_sql_examples(
        "How many customers?", top_k=3
    )
    assert len(examples) > 0, "Should find similar cases"
    assert all(isinstance(q, str) and isinstance(sql, str) 
               for q, sql in examples), "Examples should be (question, sql) pairs"
```

### Validation Checklist
- [ ] No new dependencies
- [ ] Existing tests still pass
- [ ] Few-shot examples appear in logs (debug output)
- [ ] Benchmark case with known SQL is used as example
- [ ] No performance regression (<200ms overhead)

---

## 2️⃣ **PRIORITY 2: Tunable Retrieval Top-K** ⭐⭐

**Current State:** Top-K (number of hits to retrieve from Pinecone) is hard-coded or missing.

**Impact:**
- Operators can dial precision (fewer hits, faster) vs. recall (more hits, slower)
- Enables A/B testing retrieval depth
- Better per-use-case tuning

**Effort:** 30 minutes | **Risk:** Negligible

### Implementation

#### Step 1: Add setting
```python
# src/rag_system/config.py

class Settings(BaseSettings):
    # ... existing ...
    
    retrieval_top_k: int = Field(
        10,
        ge=1,
        le=100,
        description="Number of top results to retrieve from vector store"
    )
```

#### Step 2: Find & update PineconeHybridIndex.search()
```python
# src/rag_system/retrieval.py

def search(self, embedding: list[float], top_k: int | None = None) -> list[RetrievalHit]:
    """Search the index for similar chunks."""
    if top_k is None:
        # This is where top_k might be hard-coded; make it a parameter
        top_k = 10  # old default
    
    # Use the passed-in top_k
    results = self._index.query(
        vector=embedding,
        top_k=top_k,  # ← pass it through
        include_metadata=True
    )
    # ... parse results ...
    return hits
```

#### Step 3: Update RagService._retrieve()
```python
# src/rag_system/service.py

def _retrieve(
    self, 
    request: QueryRequest, 
    recorder: Any,
    retrieval_mode: str,
    log_extra: dict[str, Any]
) -> list[RetrievalHit]:
    # ... existing embedding code ...
    
    # NEW: Pass top_k from settings
    with recorder.record_span("retrieval"):
        top_hits = self.index.search(
            embedding, 
            top_k=self._settings.retrieval_top_k  # ← use setting
        )
```

#### Step 4: Test
```python
# tests/test_retrieval_top_k.py

def test_retrieval_top_k_setting():
    """Verify top_k is configurable."""
    settings = Settings(retrieval_top_k=20)
    service = RagService(settings)
    
    # Mock index to capture the top_k argument
    original_search = service.index.search
    captured_top_k = []
    
    def mock_search(embedding, top_k=10):
        captured_top_k.append(top_k)
        return original_search(embedding, top_k=top_k)
    
    service.index.search = mock_search
    
    response = service.query(QueryRequest(question="test"))
    
    assert captured_top_k[-1] == 20, f"Expected top_k=20, got {captured_top_k[-1]}"
```

### Validation Checklist
- [ ] Setting appears in config docs
- [ ] Default is reasonable (10–20)
- [ ] Bounds check (1–100)
- [ ] Works with both dense and hybrid retrieval
- [ ] Metric added: `retrieval_top_k` labeled with the config value

---

## 3️⃣ **PRIORITY 3: Clean Up Expired Refresh Tokens** ⭐⭐

**Current State:** Refresh tokens accumulate forever in the database.

**Impact:**
- ↓ Unbounded token table growth
- ↓ Faster listing queries on the table
- ✅ Cleaner audit trail

**Effort:** 1 hour | **Risk:** Low

### Implementation

#### Step 1: Add scheduler job
```python
# src/rag_system/retention.py (new file)

import asyncio
from datetime import datetime, timedelta, timezone
from rag_system.config import Settings
from rag_system.observability import get_logger

logger = get_logger(__name__)

async def cleanup_expired_refresh_tokens(settings: Settings) -> int:
    """Delete refresh tokens expired >7 days ago.
    
    Returns the number of tokens deleted.
    """
    import neon_db  # or your DB wrapper
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    cutoff_iso = cutoff.isoformat()
    
    result = await neon_db.execute(
        "DELETE FROM refresh_tokens WHERE expires_at < %s",
        (cutoff_iso,)
    )
    
    deleted_count = result.rowcount
    logger.info(
        "Cleanup: deleted %d expired refresh tokens (>7 days old)",
        deleted_count
    )
    return deleted_count

async def run_cleanup_loop(settings: Settings) -> None:
    """Run cleanup job daily."""
    import asyncio
    
    while True:
        try:
            await cleanup_expired_refresh_tokens(settings)
        except Exception:
            logger.warning("Cleanup job failed", exc_info=True)
        
        # Sleep 24 hours
        await asyncio.sleep(86400)
```

#### Step 2: Start cleanup loop at app startup
```python
# src/rag_system/api.py (in the app startup)

from rag_system.retention import run_cleanup_loop

@app.on_event("startup")
async def startup():
    # ... existing startup ...
    
    # Start background cleanup job
    asyncio.create_task(run_cleanup_loop(settings))
    logger.info("Cleanup job started")
```

#### Step 3: Add test
```python
# tests/test_token_cleanup.py

async def test_cleanup_expired_refresh_tokens():
    """Verify cleanup deletes old tokens."""
    from rag_system.retention import cleanup_expired_refresh_tokens
    
    # Insert a token expired 10 days ago
    old_token_id = await db.insert_token(
        user_id="test",
        token="old",
        expires_at=datetime.now(timezone.utc) - timedelta(days=10)
    )
    
    # Insert a token expired 5 days ago
    recent_token_id = await db.insert_token(
        user_id="test",
        token="recent",
        expires_at=datetime.now(timezone.utc) - timedelta(days=5)
    )
    
    # Run cleanup (cutoff is 7 days)
    deleted = await cleanup_expired_refresh_tokens(settings)
    
    assert deleted == 1, "Should delete only the 10-day-old token"
    
    # Verify it's gone
    old_token = await db.get_token(old_token_id)
    assert old_token is None, "Old token should be deleted"
    
    # Verify recent one remains (for safety buffer)
    recent_token = await db.get_token(recent_token_id)
    assert recent_token is not None, "Recent token should be retained"
```

### Validation Checklist
- [ ] Cleanup runs once per day
- [ ] Cutoff is 7 days (configurable via `TOKEN_RETENTION_DAYS`)
- [ ] Deleted count is metered
- [ ] Job continues even if a cleanup fails
- [ ] Database index on `refresh_tokens.expires_at` exists (if not, add it)

---

## 4️⃣ **PRIORITY 4: SQL Timeout Observability** ⭐

**Current State:** `statement_timeout` is configured but not observed when it fires.

**Impact:**
- Visibility into slow/hanging queries
- Early warning for performance issues
- Better debugging

**Effort:** 30 minutes | **Risk:** None

### Implementation

```python
# src/rag_system/copilot.py

def execute(self, sql: str) -> list[dict[str, Any]]:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Install psycopg[binary] to use the database copilot.") from exc

    required = { ... }
    missing = [ ... ]
    if missing:
        raise RuntimeError(...)

    try:
        with psycopg.connect(...) as conn:
            # ... SET TRANSACTION READ ONLY ...
            # ... SET statement_timeout ...
            
            rows = conn.execute(sql).fetchmany(self._settings.copilot_max_rows)
            conn.rollback()
            return [dict(row) for row in rows]
    
    except psycopg.errors.QueryCanceled as exc:  # ← Catch timeout
        # Statement timeout fires as QueryCanceled
        metrics.increment("rag_copilot_statement_timeout_total")
        logger.warning(
            "SQL statement timeout (%.0f ms)",
            self._settings.copilot_statement_timeout_ms,
            exc_info=True
        )
        raise RuntimeError(
            f"Query exceeded {self._settings.copilot_statement_timeout_ms}ms timeout"
        ) from exc
```

### Validation Checklist
- [ ] Metric name: `rag_copilot_statement_timeout_total` (counter)
- [ ] Logged at WARNING level with context
- [ ] Raised as `RuntimeError` (converted to API error 500)
- [ ] Test: mock timeout and verify metric is incremented

---

## 5️⃣ **PRIORITY 5: Reranker Disabled Warning** ⭐

**Current State:** When `rerank_enabled=False`, reranking is silently skipped.

**Impact:**
- Operators know ranking quality is degraded
- Prompts investigation if answers are poor

**Effort:** 30 minutes | **Risk:** None

### Implementation

```python
# src/rag_system/service.py

@property
def reranker(self) -> BedrockCohereReranker | None:
    if not self._settings.rerank_enabled:
        # Log once at creation, not on every query
        if not hasattr(self, '_reranker_warning_logged'):
            logger.warning(
                "Reranking is disabled; answer quality may be degraded "
                "(set RERANK_ENABLED=true to enable Cohere reranking)"
            )
            self._reranker_warning_logged = True
        return None
    
    if self._reranker is None:
        self._reranker = BedrockCohereReranker(self._settings)
    return self._reranker
```

Alternatively, surface it in the API response:
```python
# In QueryResponse model (models.py)
class QueryResponse(BaseModel):
    answer: str
    # ... existing fields ...
    reranking_enabled: bool = True  # NEW
```

---

## 6️⃣ **PRIORITY 6: Stream Query Integration Test** ⭐

**Current State:** `query_stream()` is tested indirectly; no dedicated integration test.

**Impact:**
- Confidence that streaming works end-to-end
- Catches regressions in streaming paths

**Effort:** 1 hour | **Risk:** Low

### Implementation

```python
# tests/test_query_stream_integration.py

import pytest
from rag_system.models import QueryRequest

@pytest.mark.asyncio
async def test_query_stream_yields_all_events():
    """Verify streaming query emits status → text → final."""
    service = await setup_rag_service_with_mock_chunks()
    request = QueryRequest(question="What is X?")
    
    events = []
    async for event in service.query_stream(request):
        events.append(event)
    
    # Verify event sequence
    event_types = [e.get("type") for e in events]
    assert "status" in event_types, "Should emit status event"
    assert "delta" in event_types, "Should emit text deltas"
    assert event_types[-1] == "final", "Last event should be final"
    
    # Verify final response
    final_response = events[-1]["response"]
    assert final_response.answer, "Answer should not be empty"
    assert final_response.confidence_score > 0, "Should have confidence"
    assert len(final_response.citations) > 0, "Should have citations"

@pytest.mark.asyncio
async def test_query_stream_with_zero_hits():
    """Verify streaming works when no results are found."""
    service = await setup_rag_service_with_empty_index()
    request = QueryRequest(question="What is X?")
    
    events = list(service.query_stream(request))
    final_response = events[-1]["response"]
    
    assert final_response.evidence_status == "no_rows"
    assert final_response.confidence_score < 0.4
    assert "no matching" in final_response.answer.lower()
```

---

## 7️⃣ **PRIORITY 7: Delete Document — GC Chunks** ⭐

**Current State:** When a document is deleted, its chunks, embeddings, and parsed content remain in GCS.

**Impact:**
- ↓ Unbounded GCS storage growth
- ✅ Full cleanup on document deletion
- Better cost control

**Effort:** 1 hour | **Risk:** Low

### Implementation

```python
# src/rag_system/service.py

def delete_document(self, document_id: str) -> DocumentRecord | None:
    record = self.get_document(document_id)
    if record is None:
        return None
    if record.status == DocumentStatus.deleted:
        return record

    with timed(logger, "Pinecone document delete", document_id=document_id):
        self.index.delete_document(document_id)
    
    # NEW: Delete all GCS artifacts for this document
    self._cleanup_document_artifacts(document_id)

    deleted = record.model_copy(update={"status": DocumentStatus.deleted, "error": None})
    self._save_document_record(deleted)
    metrics.increment("rag_documents_deleted_total")
    logger.info("Document deleted", extra={"document_id": document_id, "version": record.version})
    return deleted

def _cleanup_document_artifacts(self, document_id: str) -> None:
    """Delete all GCS artifacts for a document (chunks, embeddings, parsed)."""
    if not hasattr(self._store, "delete"):
        logger.warning("Store does not support deletion; artifacts not cleaned")
        return
    
    # List all versions for this document
    index = self._load_version_index(document_id)
    if index is not None:
        for version_entry in index.versions:
            version = version_entry.version
            # Delete chunks
            try:
                self._store.delete(chunks_key(document_id, version))
            except Exception:
                logger.warning(f"Failed to delete chunks for {document_id}/{version}")
            
            # Delete parsed content
            try:
                self._store.delete(parsed_key(document_id, version))
            except Exception:
                logger.warning(f"Failed to delete parsed for {document_id}/{version}")
            
            # Delete embedding manifest
            try:
                self._store.delete(embedding_manifest_key(document_id, version))
            except Exception:
                logger.warning(f"Failed to delete manifest for {document_id}/{version}")
    
    # Delete raw documents (by listing prefix)
    try:
        for key in self._store.list_raw_document_keys(document_id):
            self._store.delete(key)
    except Exception:
        logger.warning(f"Failed to delete raw documents for {document_id}")
    
    logger.info("Cleaned up GCS artifacts for document", extra={"document_id": document_id})
```

### Validation Checklist
- [ ] Cleanup is logged (no silent failures)
- [ ] Each artifact type has independent error handling
- [ ] Store's `delete()` method exists and is idempotent
- [ ] Test verifies chunks are gone post-deletion

---

## 🎬 Rollout Plan

### Phase 1: Foundation (Week 1)
1. Few-shot SQL generation (1)
2. Tunable retrieval top-K (2)
3. Cleanup tokens (3)

**Testing:** Run full suite, dogfood on eval set.

### Phase 2: Polish (Week 2)
4. SQL timeout observability (4)
5. Reranker disabled warning (5)
6. Stream query test (6)

**Testing:** Verify no regressions.

### Phase 3: Cleanup (Week 3)
7. Delete GC chunks (7)

**Testing:** Verify GCS cleanup.

---

## Success Metrics

| Change | Metric | Target |
|--------|--------|--------|
| Few-shot SQL | % valid SQL on first attempt | ↑ from 75% to 90% |
| Tunable top-K | Operator feedback | ✅ Can tune per use case |
| Token cleanup | Refresh token table size | ↓ 90% reduction |
| Timeout obs | Timeout incidents visible | ✅ In dashboards |
| Reranker warning | Operator awareness | ✅ In logs on startup |
| Stream test | Coverage | ↑ from 85% to 95% |
| GC chunks | GCS cost per deleted doc | ↓ by ~80% |

---

## Questions Before Implementing?

- **Few-shot SQL:** Should we use dense embeddings for similarity, or simple keyword overlap? (Embeddings = slower but more accurate)
- **Token cleanup:** Cutoff is 7 days — is that right for your rotation policy?
- **Timeout observability:** Want a dashboard panel, or is log + metric enough?

**Ready to start? Pick priority 1 (few-shot SQL) — it has the highest ROI.**
