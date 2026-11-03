# Creatio Simple Migration Toolkit (csmt) Code Review - Round 4

## Summary of Findings

1. **HIGH**: `--limit 0` exports 1 data row instead of 0 rows (`csmt/export.py:227-240`).
2. **HIGH**: Case-sensitive GUID lookup resolution in `LookupResolver` silently fails expansion and causes repeated cache-miss queries on every buffer chunk (`csmt/export.py:141, 169, 174`).
3. **MEDIUM**: `_id_literal` single-quotes braced GUIDs (`{...}`), producing HTTP 400 type mismatch errors in Creatio OData 4 (`csmt/export.py:28-35`).
4. **MEDIUM**: Missing/deleted lookup IDs are not cached as negative hits, causing redundant OData queries on every 200-row flush (`csmt/export.py:141-170`).

Project rename verification: Package renamed to `csmt`, distribution to `creatio-simple-migration-toolkit`, CLI to `csmt`. `uv build` builds clean wheel and sdist with correct entrypoint. All existing test suites pass (182 tests in `pytest -q`, 9/9 in `verify_findings_iter1.py`, 6/6 in `verify_iter2.py`).

---

## Detailed Findings

1. **Severity: HIGH**
   - **File:Line**: `csmt/export.py:227-240`
   - **What is wrong**: `ExportOptions.__post_init__` explicitly allows `--limit 0` (`if self.limit is not None and self.limit < 0: raise ConfigError`). However, in `Exporter.run()`, the limit check (`if options.limit is not None and written >= options.limit: break`) is placed *after* `pending.append(record)` and `written += 1`. On the first record, `written` becomes 1, and `1 >= 0` triggers `break`. However, `pending` now contains 1 record, and line 238 (`if pending: self._flush(pending, ...)`) writes that buffered record to the CSV file. Consequently, passing `--limit 0` exports 1 data row and reports `"rows": 1`.
   - **Why it matters**:
     1. Violates the explicit CLI flag contract (`--limit 0` must export 0 rows).
     2. Data privacy and leakage risk: A standard administrative task is executing `--limit 0` to produce an empty CSV schema/template with only headers for distribution. Because of this bug, live customer/production data from the first record is unexpectedly written into the file.
     3. Automation scripts checking export connectivity or column availability receive data rows instead of an empty set.
   - **Concrete fix**: Handle `options.limit == 0` before the loop (or at the top of the loop before buffering). If `options.limit == 0`: if `fieldnames` and `options.write_header`, write the header and return immediately with `"rows": 0` without writing any data rows:
     ```python
     if options.limit == 0:
         if options.write_header and fieldnames:
             csv.DictWriter(
                 handle,
                 fieldnames=fieldnames,
                 delimiter=options.delimiter,
                 lineterminator="\n",
             ).writeheader()
         return {
             "entity": options.entity,
             "rows": 0,
             "out": str(options.out),
             "columns": fieldnames,
             "lookups": [spec.field for spec in options.lookups],
         }
     ```

2. **Severity: HIGH**
   - **File:Line**: `csmt/export.py:141, 169, 174`
   - **What is wrong**: `LookupResolver` stores resolved captions in `bucket` using the exact string returned by Creatio (`bucket[str(record_id)] = caption`), and retrieves them using `self._bucket(spec).get(str(value))`. While Creatio evaluates `$filter=Id eq <guid>` case-insensitively, OData responses serialize primary keys in canonical lowercase (`"Id": "680a71fb-..."`). If the foreign key in the exported entity record is uppercase (e.g. `"680A71FB-..."`), `bucket.get(str(value))` returns `None` because Python dictionary key lookup is case-sensitive. Consequently, `caption()` falls back to returning the raw GUID (`str(value)`). Furthermore, in every subsequent 200-row flush, `if text in bucket` evaluates to `False` for that uppercase GUID, causing `LookupResolver` to query Creatio again for the exact same lookup ID on every flush.
   - **Why it matters**: Lookup expansion silently fails for any records whose foreign key GUIDs contain uppercase characters. In addition, large exports suffer severe performance degradation and excessive API traffic because the cache misses on every 200-row chunk for those IDs.
   - **Concrete fix**: Case-fold/normalize GUID keys when storing into `bucket`, checking `in bucket`, and reading from `bucket`:
     ```python
     def _norm_id(value: Any) -> str:
         text = str(value).strip().strip("{}")
         return text.casefold() if GUID_RE.match(text) else text
     ```
     Use `_norm_id(record_id)` when populating `bucket`, and `_norm_id(value)` when checking membership and retrieving from `bucket`.

3. **Severity: MEDIUM**
   - **File:Line**: `csmt/export.py:28-35`
   - **What is wrong**: `GUID_RE` only matches GUIDs formatted strictly as 8-4-4-4-12 hex characters without enclosing braces. If a GUID in the data contains curly braces (common in Windows, COM, and legacy Creatio database exports, e.g. `{680a71fb-ab02-4e17-b9e9-ec074cbec136}`), `GUID_RE.match()` returns `None`. `_id_literal` falls back to single-quoting the value: `'{680a...}'`. In Creatio OData 4, comparing an `Edm.Guid` column with a quoted string literal (`Id eq '{...}'`) fails with HTTP 400 `Incompatible types 'Edm.Guid' and 'Edm.String'`.
   - **Why it matters**: Any lookup field containing braced GUIDs causes the lookup query to fail with HTTP 400, aborting lookup expansion for the entire chunk.
   - **Concrete fix**: In `_id_literal`, strip `{}` and whitespace before matching `GUID_RE`:
     ```python
     def _id_literal(value: str) -> str:
         clean = str(value).strip().strip("{}")
         return clean if GUID_RE.match(clean) else f"'{odata_escape(value)}'"
     ```

4. **Severity: MEDIUM**
   - **File:Line**: `csmt/export.py:141-170`
   - **What is wrong**: In `LookupResolver.resolve()`, `bucket` is only populated for records present in the server's response (`for record in records: bucket[str(record_id)] = ...`). If an exported record references an inactive or deleted lookup entity whose ID no longer exists in Creatio, the ID is not in `records`. As a result, that ID is never added to `bucket`. In subsequent 200-row chunks, `if text in bucket` is `False`, and `LookupResolver` issues redundant `$filter` queries to Creatio for the exact same missing ID on every flush.
   - **Why it matters**: For large exports where multiple rows reference inactive or deleted lookup records, `csmt export` repeatedly sends queries to Creatio on every buffer flush for the same missing IDs, increasing network latency and server load.
   - **Concrete fix**: Record queried IDs that were not returned by the server as resolved to empty string or sentinel in `bucket` (e.g. `for val in chunk: if val not in bucket: bucket[val] = ""`), ensuring `text in bucket` prevents repeated network calls.
