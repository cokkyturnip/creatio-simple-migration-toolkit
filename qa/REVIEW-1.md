# Creatio CSV Importer Code Review

## Summary of Findings

1. **BLOCKER**: Batch failure reconciliation falsely marks updates as succeeded without executing `PATCH` (`pipeline.py:837-852`).
2. **HIGH**: Resume key mismatch when key field is a lookup causes `--resume` to ignore completed rows (`pipeline.py:323-348`, `mapping.py:355-357`, `mapping.py:444-446`).
3. **HIGH**: Resume logic swallows failed duplicate rows and falsely reports success (`journal.py:51-55`, `pipeline.py:338-350`).
4. **HIGH**: Existing record key lookup quotes GUIDs for lookup fields in `$filter` queries (`pipeline.py:245-249`, `pipeline.py:124-134`).
5. **HIGH**: Blind retries of non-idempotent `POST` requests and `$batch` on transport timeouts and 5xx errors risk duplicate record creation (`client.py:245-248`, `client.py:270-273`).
6. **MEDIUM**: Swallowing `ODataError` during lookup resolution populates negative cache and fails subsequent rows (`pipeline.py:590-618`).
7. **MEDIUM**: Header deduplication in `normalize_header` can collide and generate duplicate column names, losing data (`csv_source.py:114-125`).
8. **MEDIUM**: `parse_csdl_entity_fields` ignores `BaseType` inheritance, breaking `--validate-fields` for standard entities (`client.py:616-621`).
9. **LOW**: Default values on lookup fields bypass lookup resolution and send raw string names to GUID properties (`mapping.py:341-344`, `mapping.py:562-574`).

---

## Detailed Findings

1. **Severity: BLOCKER**
   - **File:Line**: `creatio_importer/pipeline.py:837-852`
   - **What is wrong**: In `_recover_from_batch_error()`, when a `$batch` request fails or times out, the pipeline queries Creatio to check if the record exists (`existing = self.index.get(...)`). If found, it emits `status="created" if op.action == "create" else "updated"` with message `"reconciled after batch error"` and skips execution (`continue`). Because all records targeted by an `update` operation already existed in Creatio before the batch was dispatched, `existing` is always found. The pipeline skips re-executing the `PATCH` and reports the update as successful. Additionally, line 851 converts `update` to `create` when `not existing`, violating `--mode update` semantics when `create_missing=False`.
   - **Why it matters**: Severe data loss and false reporting. Whenever a batch fails due to a server 500 error or a dropped connection, any updates in that batch are never executed against Creatio, but the report, summary, and exit code claim they succeeded.
   - **Concrete fix**: In `_recover_from_batch_error()`, distinguish between `create` and `update`. Because `PATCH` operations are idempotent, existing records for `update` operations must be re-executed (e.g. via `self._execute_one(op)`). For `create`, verify whether the record existed prior to the batch before deciding that the batch succeeded. If an update target record is missing and `not self.options.create_missing`, emit a failure instead of creating it.

2. **Severity: HIGH**
   - **File:Line**: `creatio_importer/pipeline.py:323-348`, `creatio_importer/mapping.py:355-357`, `creatio_importer/mapping.py:444-446`
   - **What is wrong**: When a key field is a lookup (e.g. `--key CountryId,UsrExternalId`), `Mapper.prepare()` populates `key_values[spec.target]` with the unresolved CSV string (e.g. `Indonesia`), yielding `dedupe_key = "CountryId=Indonesia"`. Later, `Mapper.finalize()` updates `prepared.key_values[lookup.target]` to the resolved GUID (e.g. `CountryId=6e55eb84-...`), which is recorded in the journal. On subsequent runs with `--resume`, `_consume()` prepares the row and evaluates `dedupe_key in self._completed` before lookups are resolved (`CountryId=Indonesia`), which never matches the journal entry (`CountryId=6e55eb84-...`).
   - **Why it matters**: Resume mode fails completely whenever any key field is a lookup. The importer fails to skip already-imported records and re-runs them, causing duplicate key conflicts or redundant updates.
   - **Concrete fix**: Maintain consistent key representations across runs: either compute the dedupe/resume key using the raw CSV source values before lookup resolution, or resolve lookups before checking `self._completed`. Ensure `Mapper.finalize()` does not alter the key format used by `self._completed`.

3. **Severity: HIGH**
   - **File:Line**: `creatio_importer/journal.py:51-55` & `creatio_importer/pipeline.py:338-350`
   - **What is wrong**: `Journal.load_completed()` loads entries into a dictionary keyed solely by `key` whenever `status in SUCCESS_STATUSES`. In `_consume()`, any row where `dedupe_key in self._completed` is skipped as `"already completed in a previous run"`. If a CSV contains multiple rows with the same key (e.g. duplicate keys under `--duplicate-policy error`), row 1 succeeds (`created`) and row 2 fails (`failed`). On `--resume`, because row 1 populated `self._completed[key]`, row 2 matches `self._completed` and is skipped as succeeded, yielding 0 failures and exit code 0.
   - **Why it matters**: Data loss and false success reporting. Failed duplicate rows from previous runs are silently masked as successful on resume rather than flagged or retried.
   - **Concrete fix**: Key the journal completion index by both row line number and dedupe key (e.g. `(row_lineno, dedupe_key)`), or ensure that subsequent failure entries in the journal invalidate prior success entries for that row.

4. **Severity: HIGH**
   - **File:Line**: `creatio_importer/pipeline.py:245-249` & `creatio_importer/pipeline.py:124-134`
   - **What is wrong**: `quote_fields` in `ImportPipeline.__init__` is constructed as `{spec.target: (spec.type != "guid") ...}`. For lookup fields, `spec.type` is `"lookup"`, so `spec.type != "guid"` evaluates to `True`. Consequently, `ExistingIndex._literal()` wraps the resolved GUID in single quotes (`'62f9bc01-...'`) instead of formatting it as an unquoted OData 4 GUID literal.
   - **Why it matters**: In Creatio OData 4, comparing an `Edm.Guid` foreign key property against a single-quoted string causes an OData type mismatch error (`Incompatible types 'Edm.Guid' and 'Edm.String'`), causing existing-record lookups for lookup keys to fail with HTTP 400.
   - **Concrete fix**: Update `quote_fields` to check that the field type is neither `"guid"` nor `"lookup"`:
     ```python
     quote_fields = {
         spec.target: (spec.type not in {"guid", "lookup"})
         for spec in mapper.mapping.fields
         if spec.target in self.key_fields
     }
     ```

5. **Severity: HIGH**
   - **File:Line**: `creatio_importer/client.py:245-248` & `creatio_importer/client.py:270-273`
   - **What is wrong**: `ODataClient.request()` unconditionally retries all HTTP requests—including non-idempotent `POST` operations (`create()` and `$batch`)—on `requests.RequestException` (such as socket read timeouts) and on `RETRY_STATUSES` (408, 425, 429, 500, 502, 503, 504). If Creatio creates records but the response times out or drops (e.g. 504 Gateway Timeout or ReadTimeout), `client.request()` automatically re-transmits the `POST` up to 5 times.
   - **Why it matters**: Risk of duplicate records and violation of the design guarantee that create operations are never retried blindly. If a `$batch` containing 50 creates times out, up to 250 duplicate records can be created before `pipeline._recover_from_batch_error()` is ever invoked.
   - **Concrete fix**: Restrict automatic transport/5xx retries in `ODataClient.request()` to idempotent HTTP methods (`GET`, `PATCH`, `DELETE`). For `POST`, raise `ODataError` or `BatchError` immediately on transport timeouts or 5xx responses so higher-level reconciliation can check key existence before re-sending.

6. **Severity: MEDIUM**
   - **File:Line**: `creatio_importer/pipeline.py:590-618`
   - **What is wrong**: In `ImportPipeline._resolve_lookups()`, when a lookup collection query fails with an `ODataError` (e.g. transient 500 error or gateway timeout), the exception is swallowed with `continue`. Because `found` remains empty, the subsequent loop sets `self.lookup_cache[(entity, key_field, value_key)] = None` and `pending.resolved_id = None`.
   - **Why it matters**: Server or connection errors during lookup resolution are incorrectly cached as negative hits (`None`). All records referencing that lookup value fail validation with `ValidationError: lookup value ... was not found`, and future rows in the run are prevented from querying Creatio again.
   - **Concrete fix**: Do not swallow `ODataError` in `_resolve_lookups()`. Re-raise or record the failure on the affected rows with their actual HTTP status code, and avoid populating `self.lookup_cache` with `None` when the lookup query encountered an error.

7. **Severity: MEDIUM**
   - **File:Line**: `creatio_importer/csv_source.py:114-125`
   - **What is wrong**: `normalize_header()` handles duplicate column headers by appending `_{seen[name]}` without verifying whether the resulting name already exists in the header. For an input like `["Name", "Name_2", "Name"]`, the first column is `Name`, the second is `Name_2`, and the third is renamed to `Name_2` because `seen["Name"]` equals 2.
   - **Why it matters**: Generates duplicate column names (`['Name', 'Name_2', 'Name_2']`). When rows are mapped into dictionaries in `CsvSource.iter_rows()`, column 3 overwrites column 2, silently discarding CSV data.
   - **Concrete fix**: Ensure candidate names are tested in a loop until unique:
     ```python
     candidate = name
     while candidate in seen:
         seen[name] += 1
         candidate = f"{name}_{seen[name]}"
     seen[candidate] = 1
     names.append(candidate)
     ```

8. **Severity: MEDIUM**
   - **File:Line**: `creatio_importer/client.py:616-621`
   - **What is wrong**: `parse_csdl_entity_fields()` only collects `<Property>` tags directly under the target `<EntityType>` element and ignores the `BaseType` attribute. In Creatio CSDL metadata, standard entities inherit common properties (such as `Id`, `CreatedOn`, `ProcessListeners`) from `BaseEntity`.
   - **Why it matters**: Running `import --validate-fields` or `schema` omits inherited fields. When `--validate-fields` is enabled, mappings targeting standard columns like `Id` or `ProcessListeners` fail validation with `MappingError: Mapping targets field(s) that do not exist on the entity`.
   - **Concrete fix**: In `parse_csdl_entity_fields()`, check for the `BaseType` attribute on `EntityType`. If present, locate the base entity type in `entity_types` and recursively collect inherited `<Property>` elements into `fields`.

9. **Severity: LOW**
   - **File:Line**: `creatio_importer/mapping.py:341-344` & `creatio_importer/mapping.py:562-574`
   - **What is wrong**: When an empty CSV column has a configured `default` on a field with `type: "lookup"`, `Mapper.prepare()` executes `value = self._coerce(spec, spec.default)`. Because `"lookup"` is not registered in `_CONVERTERS`, `_coerce()` falls back to `_to_string`, placing the raw default string directly into `payload[spec.target]`. It never instantiates a `PendingLookup`.
   - **Why it matters**: If a mapping defines a default for a lookup field by name (e.g. `"default": "Indonesia"`), the name is never resolved to a GUID and is sent directly in the OData JSON body, causing Creatio to return HTTP 400 Bad Request.
   - **Concrete fix**: In `Mapper.prepare()`, when `spec.type == "lookup"` and `is_empty` with `spec.default is not None`, instantiate a `PendingLookup` for the default string (unless it is already a valid GUID) so it is properly resolved in `_resolve_lookups()`.
