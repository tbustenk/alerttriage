# AlertTriage v2 — Performance Guide

## Table of Contents

- [Architecture Performance Notes](#architecture-performance-notes)
- [SQLite Tuning](#sqlite-tuning)
- [Caching Strategy](#caching-strategy)
- [Async Concurrency Settings](#async-concurrency-settings)
- [Benchmarks](#benchmarks)
- [Scaling to 1,000+ Alerts/Day](#scaling-to-1000-alertsday)
- [Memory Usage Guidelines](#memory-usage-guidelines)

---

## Architecture Performance Notes

AlertTriage is designed for correctness and operational simplicity over raw throughput. It is not a message-queue-based pipeline — it processes alerts synchronously through an async Python event loop. This design makes it easy to operate and debug, and it is more than fast enough for typical SOC workloads.

**Hot path for `POST /analyze`:**

```
HTTP parse              ~1ms
Rate limiter check      <0.1ms (in-memory deque)
Alert model validation  ~1ms   (Pydantic v2)
Cost limit check        <1ms   (in-memory cache + JSON file read)
PII anonymisation       ~2ms   (regex + hash — CPU-bound but fast)
Prompt enhancement      <1ms   (in-memory TTL cache hit)
  └─ if cache miss:     ~5ms   (SQLite aggregation query)
AI backend call         50-3000ms (Anthropic/OpenAI network + inference)
Cost recording          <1ms   (atomic file write)
Result caching          <0.1ms (dict insert)
Webhook emission        <0.1ms (fire-and-forget async task)
HTTP response           ~1ms
```

The AI backend call dominates. All other components add less than 10ms combined. The target for end-to-end p95 latency is **under 2,000ms** (including Anthropic inference).

---

## SQLite Tuning

SQLite is the storage layer for analyst feedback. The default configuration is appropriate for most deployments. For higher-volume environments, apply the following settings.

### WAL Mode

Write-Ahead Logging allows concurrent reads while a write is in progress. The `FeedbackSystem` enables WAL mode automatically when it initialises the database.

To verify:

```bash
sqlite3 data/acme-corp/feedback.db "PRAGMA journal_mode;"
# Should return: wal
```

To set manually on an existing database:

```bash
sqlite3 data/acme-corp/feedback.db "PRAGMA journal_mode=WAL;"
```

### Cache Size

The default SQLite page cache is 2MB. For large feedback databases (>100k rows), increase it:

```python
# Applied in FeedbackSystem._conn() — add to the connection setup:
conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
```

### Checkpoint Configuration

WAL files grow until they are checkpointed. The default auto-checkpoint at 1,000 pages is appropriate. For write-heavy workloads:

```bash
sqlite3 data/acme-corp/feedback.db "PRAGMA wal_autocheckpoint = 2000;"
```

### Indexes

The schema ships with five indexes covering the most common query patterns used by the LearningEngine and analytics:

```sql
idx_feedback_client    ON feedback(client_id)
idx_feedback_alert     ON feedback(alert_id)
idx_feedback_rule      ON feedback(client_id, rule_name)
idx_feedback_verdict   ON feedback(client_id, analyst_verdict)
idx_feedback_timestamp ON feedback(client_id, timestamp DESC)
```

The composite `(client_id, rule_name)` index is the most important — the learning engine's aggregation query hits this index directly without touching the `metadata` JSON column.

**Verify index usage:**

```bash
sqlite3 data/acme-corp/feedback.db "
  EXPLAIN QUERY PLAN
  SELECT rule_name, COUNT(*), SUM(ai_correct), SUM(CASE WHEN analyst_verdict='false_positive' THEN 1 ELSE 0 END)
  FROM feedback WHERE client_id = 'acme-corp'
  GROUP BY rule_name;
"
# Should show: SEARCH feedback USING INDEX idx_feedback_rule
```

### Periodic Maintenance

Run `ANALYZE` after large bulk imports to update query planner statistics:

```bash
sqlite3 data/acme-corp/feedback.db "ANALYZE; PRAGMA optimize;"
```

---

## Caching Strategy

AlertTriage uses three in-memory caches, all without external dependencies:

### Result Cache (`_result_store`)

**What:** `AnalysisResult` objects keyed by `alert_id`.  
**Size:** LRU, capped at 10,000 entries.  
**TTL:** None — entries live until evicted by the LRU or a server restart.  
**Purpose:** Powers `GET /results/{alert_id}` without re-querying the AI.

**Memory estimate:** Each result is ~2-5KB. At 10,000 entries: 20-50MB maximum.

This cache is process-local. Multiple replicas will not share it. If you need cache sharing across replicas, consider routing `GET /results/{alert_id}` to the same replica that handled the original `POST /analyze` (sticky sessions) or use an external cache.

---

### Prompt Enhancement Cache (`PromptEnhancer`)

**What:** The compiled system prompt (base + hints) per client.  
**TTL:** Configurable via `learning.cache_ttl_sec` (default 300 seconds).  
**Invalidated:** On every `POST /feedback` call for the client.

This cache is critical for Anthropic prompt caching. Claude's prompt caching feature gives a significant cost reduction (up to 90% on cached prefixes) when the system prompt is stable across calls. The 300-second TTL ensures the prompt stays warm in Anthropic's cache while still picking up new feedback hints promptly.

**Tuning:** If you are recording feedback very frequently (>1 per minute) and see low Anthropic cache hit rates, increase the TTL:

```yaml
learning:
  cache_ttl_sec: 600  # 10 minutes
```

---

### Rate Limiter (`RateLimiter`)

**What:** Per-IP timestamp deques.  
**Algorithm:** Sliding 60-second window.  
**Memory:** One `deque` per unique source IP. Each entry is a float timestamp (8 bytes). At 60 req/min and 1,000 unique IPs: ~480KB maximum.

---

## Async Concurrency Settings

The async event loop is Python's `asyncio`. The only blocking operations are:

1. SQLite reads/writes — these are synchronous but fast (<5ms typically).
2. File I/O for cost state — atomic writes are synchronous.

All AI backend calls are fully async via `httpx.AsyncClient`.

### `concurrency.max_in_flight`

Controls the maximum number of simultaneous AI calls when using `analyze_batch()`.

```yaml
concurrency:
  max_in_flight: 5   # default — appropriate for most workloads
```

Each in-flight call holds one HTTP connection and consumes ~2MB of RAM. Increasing this improves batch throughput at the cost of higher memory usage and potential Anthropic rate limiting.

**Recommended values by workload:**

| Workload | `max_in_flight` | Notes |
|----------|----------------|-------|
| Dev / testing | 2 | Prevents accidental large spend |
| Standard SOC (< 500 alerts/day) | 5 | Default |
| High volume (500-5,000/day) | 10-15 | Watch Anthropic rate limits |
| Very high volume (> 5,000/day) | 20+ | Requires Tier 2+ Anthropic plan |

### Uvicorn Worker Configuration

The API runs a single uvicorn worker by default. For a multi-core machine:

```bash
# In scripts/run_api.py or your systemd unit:
uvicorn alerttriage.src.api.app:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 4  # one per CPU core
```

**Note:** With multiple workers, the in-memory result cache, rate limiter, and prompt cache are per-worker (not shared). This is acceptable for the cache and prompt enhancer. For rate limiting, the effective limit becomes `ALERTTRIAGE_API_RATE_LIMIT × workers`. If precise rate limiting is needed, place a rate limiter at the load balancer or reverse proxy level.

---

## Benchmarks

These are representative measurements on a `c5.xlarge` (4 vCPU, 8GB RAM) running the Docker container with a Splunk-sourced alert.

### API Latency (single worker, no concurrency)

| Percentile | Target | Typical |
|------------|--------|---------|
| p50 | < 1,000ms | 800-1,200ms |
| p95 | < 2,000ms | 1,500-2,500ms |
| p99 | < 5,000ms | 3,000-6,000ms |

AI inference dominates. The non-AI components add less than 10ms.

### `POST /analyze` Throughput

| Configuration | Throughput |
|---------------|-----------|
| `max_in_flight=5`, 1 worker | ~15 req/min |
| `max_in_flight=10`, 1 worker | ~25 req/min |
| `max_in_flight=10`, 4 workers | ~80 req/min |

For 1,000 alerts/day: ~0.7 req/min average — easily handled by a single worker with `max_in_flight=5`.

### SQLite Operations

| Operation | Typical latency |
|-----------|----------------|
| Single `INSERT` (FeedbackSystem.record) | < 2ms |
| Batch `INSERT` (100 rows) | < 20ms |
| `rule_stats()` aggregation (10k rows) | < 5ms (index hit) |
| `accuracy()` query | < 2ms |
| Analytics report (50k rows, 30 days) | 50-200ms |

### Prompt Enhancement Cache

| Condition | Latency |
|-----------|---------|
| Cache hit (< 300s old) | < 0.1ms |
| Cache miss (first call or after feedback) | 3-8ms (SQLite aggregation) |

---

## Scaling to 1,000+ Alerts/Day

### 1,000 alerts/day (~40/hour)

Default configuration handles this comfortably on a single instance. No tuning needed.

- Estimated daily AI cost: $5-15 (at claude-sonnet pricing)
- SQLite performance: negligible load
- Recommended: Set `cost_limits.daily_usd` to match your budget

### 5,000 alerts/day (~200/hour)

- Increase `concurrency.max_in_flight` to 10
- Use `analyze_batch()` from the CLI runner rather than individual API calls
- Ensure `/data` is on SSD-backed storage
- Monitor Anthropic rate limits — consider applying for a higher tier

### 10,000+ alerts/day

- Run 2-4 API replicas with client-affinity load balancing (each client always routes to the same replica)
- Increase `concurrency.max_in_flight` to 15-20 per replica
- Add SQLite tuning: `cache_size = -128000`, `wal_autocheckpoint = 2000`
- Schedule analytics queries during off-peak hours to avoid read/write contention
- Consider read replicas for analytics: copy `feedback.db` to a read-only host for heavy reporting

### Database Size Projections

Each feedback record is approximately 400-600 bytes on disk.

| Feedback records | Database size |
|-----------------|--------------|
| 10,000 | ~5 MB |
| 100,000 | ~50 MB |
| 1,000,000 | ~500 MB |

SQLite performs well up to several GB. For databases larger than 1GB, consider archiving records older than 180 days.

---

## Memory Usage Guidelines

| Component | Memory usage |
|-----------|-------------|
| API process baseline | ~80MB |
| Per-client FeedbackSystem | ~2MB (connection pool, schema cache) |
| Result cache (full, 10k entries) | ~30-50MB |
| Rate limiter (1,000 unique IPs) | ~0.5MB |
| Prompt cache (10 clients) | ~2MB |
| Per in-flight AI call | ~2MB |

**Total for a typical deployment** (5 clients, `max_in_flight=5`):
- Baseline: ~80MB
- Clients: ~10MB
- In-flight calls: ~10MB
- Caches: ~55MB
- **Total: ~155MB**

**Recommended container memory limit:** 512MB for up to 10 clients with `max_in_flight=10`.

If memory exceeds 400MB, check:

1. The result cache is filling up — this is expected and bounded at 10,000 entries.
2. Long-running batch jobs with large `raw_payload` fields — truncate payloads before submission.
3. Memory leaks in custom extensions — use `tracemalloc` to profile.
