# Creatio CSV Importer Code Review - Iteration 2

## Summary of Findings

1. **BLOCKER**: Setting `prepared.key_values[lookup.target] = lookup.value` causes `ExistingIndex` to query Creatio with raw display text (e.g. `CountryId eq Indonesia`) instead of resolved GUIDs, completely breaking existing-record matching for lookup keys (`mapping.py:478-485`, `pipeline.py:135-139, 452, 531`).
2. **BLOCKER**: In `_reconcile_failed_create`, calling `self.index.remember(key, None)` immediately before `preload()` caches `None`, causing `preload()` to skip querying Creatio and permanently disabling create reconciliation (`pipeline.py:755`, `pipeline.py:165`).
3. **HIGH**: Pure physical-row resume (`row.lineno in self._completed`) without key validation causes silent data loss and duplicate record creation whenever CSV rows shift or are edited between runs (`journal.py:32-68`, `pipeline.py:349-360`).
4. **HIGH**: Resumed rows are skipped without registering their `dedupe_key` into `self._seen`, allowing subsequent duplicate rows in the CSV to bypass duplicate-policy detection (`pipeline.py:349-360`, `pipeline.py:415`).
5. **MEDIUM**: Disallowing retries for non-idempotent methods prevents HTTP 429 (Rate Limited) from honoring `Retry-After` on `POST` and `$batch` requests (`client.py:278-285`).

---

## Detailed Findings

1. **Severity: BLOCKER**
   - **File:Line**: `creatio_importer/mapping.py:478-485` (and effects in `creatio_importer/pipeline.py:135-139, 452, 531`)
   - **What is wrong**: In `Mapper.finalize()`, line 484 sets `prepared.key_values[lookup.target] = lookup.value` (e.g. `"Indonesia"`). When `ExistingIndex.preload()` and `ExistingIndex.get()` check Creatio for existing records, `ExistingIndex._clause()` formats the filter using `key_values`:
     `f"{name} eq {self._literal(name, key_values[name])}"`.
     Because `spec.type` is `"lookup"` and `quote_fields` was updated to unquote lookups (`spec.type not in {"guid", "lookup"}`), `ExistingIndex` queries Creatio with:
     `GET Contact?$filter=(CountryId eq Indonesia)`.
     In Creatio, `Contact.CountryId` is an `Edm.Guid` foreign key containing GUIDs, not country names.
   - **Why it matters**: `ExistingIndex` can never find any existing record when a key field is a lookup.
     - In `upsert` mode, existing records are never found; it always attempts a `POST`, creating duplicate records or failing with unique constraint violations.
     - In `update` mode, it always fails with `"no record found for key CountryId='Indonesia'"`.
     - In `create --skip-existing`, it fails to detect existing records and creates duplicates.
     - On real Creatio OData 4 services, sending an unquoted string literal `Indonesia` for a GUID property fails with HTTP 400 Bad Request.
   - **Concrete fix**: `ExistingIndex` must query Creatio using the resolved GUID (`prepared.payload[lookup.target]`), not the raw CSV string. Separate the key representation used for journal reporting from the key values dictionary passed to `ExistingIndex`.

2. **Severity: BLOCKER**
   - **File:Line**: `creatio_importer/pipeline.py:755` & `creatio_importer/pipeline.py:165`
   - **What is wrong**: In `_reconcile_failed_create()`, line 755 calls `self.index.remember(op.prepared.key_values, None)`, which sets `self.index.cache[cache_key] = None`. Line 757 then calls `self.index.preload(iter([dict(op.prepared.key_values)]))`.
     In `ExistingIndex.preload()`, line 165 checks:
     `if cache_key in self.cache or cache_key in seen: continue`.
     Because `cache_key` was added to `self.index.cache` on line 755, `preload()` sees it as already cached and skips querying Creatio. Line 761 calls `existing = self.index.get(...)`, which immediately returns `None`, and line 762 returns `None`.
   - **Why it matters**: `_reconcile_failed_create()` is completely incapacitated. If a single `POST` creates a record in Creatio but the client encounters a read timeout or dropped socket, `_reconcile_failed_create()` never queries Creatio and reports the row as `failed`. On `--resume`, the importer re-attempts creation, resulting in duplicate records or unique constraint failures.
   - **Concrete fix**: In `_reconcile_failed_create()`, evict the cache entry using `self.index.cache.pop(cache_key, None)` instead of `self.index.remember(op.prepared.key_values, None)` so that `preload()` actually queries Creatio.

3. **Severity: HIGH**
   - **File:Line**: `creatio_importer/journal.py:32-68` & `creatio_importer/pipeline.py:349-360`
   - **What is wrong**: In the iteration 1 fix, `Journal.load_completed()` was changed to index exclusively by `row_lineno` (`completed: dict[int, dict]`), and `_consume()` resumes rows purely on `row.lineno in self._completed`. It never checks whether the current row's key matches the key recorded for that line number in the journal.
   - **Why it matters**: If a user edits the CSV between runs (e.g. deleting a failed row, inserting a row, or re-ordering lines):
     - Unprocessed rows that now sit on line numbers that previously succeeded are silently skipped as "already completed", causing silent data loss.
     - Previously completed rows that shifted to new line numbers are re-executed, causing duplicate records in Creatio.
   - **Concrete fix**: Validate that the row's `dedupe_key` matches the journal entry before skipping:
     ```python
     if self.options.resume and row.lineno in self._completed:
         completed_entry = self._completed[row.lineno]
         if completed_entry.get("key") == dedupe_key:
             self._emit(RowResult(lineno=row.lineno, dedupe_key=dedupe_key, action="resume", status="skipped", error="already completed in a previous run"), resumed=True)
             self._seen[dedupe_key] = row.lineno
             continue
     ```

4. **Severity: HIGH**
   - **File:Line**: `creatio_importer/pipeline.py:349-360` & `creatio_importer/pipeline.py:415`
   - **What is wrong**: When a row is skipped via `--resume`, line 360 executes `continue` before line 415 (`self._seen[dedupe_key] = row.lineno`). Resumed rows are never recorded in `self._seen`.
   - **Why it matters**: If an uncompleted row later in the CSV shares a key with a previously completed row (e.g. line 2 completed in run 1, and line 50 has the same key), line 50 calls `self._seen.get(dedupe_key)`, receives `None`, and bypasses `--duplicate-policy error` / `first_wins`. It is dispatched to Creatio as a new write, creating duplicate records or overwriting data.
   - **Concrete fix**: When skipping a completed row during resume, register its key in `self._seen`:
     ```python
     if self.options.resume and row.lineno in self._completed:
         self._seen[dedupe_key] = row.lineno
         self._emit(...)
         continue
     ```

5. **Severity: MEDIUM**
   - **File:Line**: `creatio_importer/client.py:278-285`
   - **What is wrong**: In `ODataClient.request()`, `IDEMPOTENT_METHODS = {"GET", "PATCH", "DELETE"}`. When Creatio responds with HTTP 429 (Too Many Requests), line 279 checks `method in IDEMPOTENT_METHODS`. For `POST` and `$batch`, it immediately raises `ODataError` instead of backing off and retrying.
   - **Why it matters**: HTTP 429 is a rate limiter rejection sent before the server executes the write; no data has been created or modified. The README specifically advertises backoff honoring `Retry-After` for 429. Aborting on 429 prevents large imports from completing under Creatio rate limits.
   - **Concrete fix**: Allow HTTP 429 responses to be retried regardless of HTTP method:
     ```python
     if status == 429 or (status in RETRY_STATUSES and method in IDEMPOTENT_METHODS):
         if retries < self.settings.max_retries:
             retries += 1
             self._backoff(retries, response.headers.get("Retry-After"), None)
             continue
     ```
