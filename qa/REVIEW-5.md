# Creatio Simple Migration Toolkit (`csmt`) Code Review - Round 4, Iteration 2 (Final)

## Status: READY TO SHIP

All findings from `REVIEW-4.md` have been verified fixed without regressions or new bugs. The codebase is clean, robust, and production-ready.

- Automated test suite: **186 passed** (`uv run pytest -q`).
- Verification suites: **9/9 PASS** (`verify/verify_findings_iter1.py`), **6/6 PASS** (`verify/verify_iter2.py`).
- Adversarial test suite (`/tmp/verify_r4_adversarial.py`): **ALL PASSED**.
  - `--limit 0` writes 0 data rows and performs 0 network requests; emits header when `--select` is specified.
  - `--limit 1` writes header and exactly 1 data row.
  - Uppercase and braced GUIDs (`{680A71FB-...}` vs canonical `680a71fb-...`) normalize cleanly via `clean_guid()` and `id_key()`, resolving correctly against the cache.
  - Non-existent / deleted lookup IDs are cached in `_missing` and are not re-queried across the 200-row flush boundaries; raw IDs are preserved in output for data integrity.
  - Negative cache does not hide valid records (even records with `null` or empty captions are correctly distinguished and preserved as empty cells).
  - Multi-chunk lookup lookups (>30 and >70 GUIDs) correctly batch into OData `$filter` chunks without exceeding URL limits.
  - Roundtrip fidelity: Multiline text, double quotes, commas, unicode characters (CJK), ISO dates, decimals, and booleans roundtrip through CSV without corruption.

---

## Verification of REVIEW-4 Findings

1. **Fixed (F1 - HIGH): `--limit 0` writes 0 data rows**
   - **Verification**: `Exporter.run()` contains an up-front guard `if options.limit == 0:`. When `--select` is supplied, only the CSV header is written to the destination file. No network requests are made to Creatio, allowing instant offline template generation.
2. **Fixed (F2 - HIGH): Case-insensitive lookup cache for GUIDs**
   - **Verification**: `clean_guid()` strips braces/spaces and matches canonical GUID format; `id_key()` applies `casefold()`. Both lookup querying, cache indexing, and cache retrieval use `id_key()`, preventing uppercase foreign keys from causing duplicate queries or unexpanded captions.
3. **Fixed (F3 - MEDIUM): Braced GUIDs unquoted in `$filter`**
   - **Verification**: `_id_literal()` uses `clean_guid()`, emitting clean unquoted GUIDs (e.g. `Id eq 680a71fb-ab02-...`), satisfying Creatio OData 4 syntax without triggering HTTP 400 type mismatch errors.
4. **Fixed (F4 - MEDIUM): Negative caching for missing/deleted lookup records**
   - **Verification**: `LookupResolver._missing` tracks non-existent lookup IDs separately from `_cache`. Subsequent 200-row buffer flushes skip querying known missing IDs while `caption()` preserves raw GUIDs in the exported CSV.

---

## Non-Blocking Advisory Notes

1. **Advisory (LOW / Non-Blocking): `--limit 0` without `--select` produces an empty file**
   - **Context**: `csmt/export.py:243-261`
   - **Details**: When running `csmt export --entity Contact --limit 0` without `--select`, the exporter writes an empty file (0 bytes) because no records or metadata are fetched to discover column names.
   - **Recommendation**: To generate an empty template CSV, pass `--select ColA,ColB,...` alongside `--limit 0`.

2. **Advisory (LOW / Non-Blocking): Cache scope per export run**
   - **Context**: `csmt/export.py:146-150`
   - **Details**: `_missing` caches nonexistent lookup IDs in memory for the duration of the export command. In extremely long exports spanning hours, if an administrator concurrently creates one of those missing lookup records in Creatio, the exporter will not query it again during that same execution. This is standard in-memory caching behavior for batch CLI tools.

3. **Advisory (LOW / Non-Blocking): Distinct caption fields on the same entity**
   - **Context**: `csmt/export.py:152-156`
   - **Details**: The cache bucket and missing set are keyed by `(spec.entity, spec.caption_field)`. Expanding multiple fields from the same lookup entity (e.g. `CountryId=Country.Name` and `CountryCode=Country.Code`) correctly partitions the caches without collision.
