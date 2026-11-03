# Creatio CSV Importer Code Review - Iteration 3 (Final)

## Status: READY TO SHIP

All BLOCKER, HIGH, and MEDIUM findings from Iteration 1 and Iteration 2 have been thoroughly verified and resolved.
- Automated tests: **125 passed** (`pytest -q`).
- Verification suites: **9/9 PASS** (`verify_findings.py`), **6/6 PASS** (`verify_iter2.py`).
- Deep adversarial checks: multi-key lookup matching, batch failure recovery, row-shift resume, and duplicate registration all passed without regression.

No blocking bugs remain. Below are non-blocking advisory notes for operational best practices.

---

## Non-Blocking Advisory Notes

1. **Advisory (LOW / Non-Blocking): Manual CSV Line Shift During Resume in `create` Mode**
   - **Context**: `creatio_importer/pipeline.py:353-376`
   - **Details**: When a CSV is manually edited between runs (e.g., rows inserted, deleted, or re-ordered), physical row numbers shift. The resume logic correctly detects that `journal[row].key != dedupe_key` and safely falls through to re-process shifted rows rather than silently skipping them. In `upsert` mode (default), re-processing shifted rows is completely self-correcting via key lookup. In `create` mode without `--skip-existing`, if a user manually shifts rows before resuming, previously created rows could be re-submitted.
   - **Recommendation**: For production runs with `--resume` after manual CSV file editing, `--mode upsert` or `--skip-existing` is recommended.

2. **Advisory (LOW / Non-Blocking): Rate Limit Header Capitalization**
   - **Context**: `creatio_importer/client.py:283`
   - **Details**: `response.headers.get("Retry-After")` uses `requests.structures.CaseInsensitiveDict` under the hood, so headers like `retry-after` and `Retry-After` are parsed properly. If the header contains an HTTP date instead of seconds, the float cast falls back cleanly to exponential backoff + jitter.

3. **Advisory (LOW / Non-Blocking): Preload URL Length Overhead**
   - **Context**: `creatio_importer/pipeline.py:151-158`
   - **Details**: In `ExistingIndex._chunks()`, `$filter` expressions are grouped up to `chunk_chars=1400` or 30 clauses per chunk. This stays safely within standard reverse proxy (IIS / Nginx / Envoy) URI length limits (typically 2048 to 4096 bytes).
