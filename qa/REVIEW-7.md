# Creatio Simple Migration Toolkit (`csmt`) Code Review - Round 6, Iteration 2 (Final)

## Status: READY TO SHIP

All findings and advisories from `REVIEW-6.md` have been verified as resolved. The implementation of Excel (`.xlsx`/`.xlsm`) tabular import and export is clean, robust, and maintains full parity with CSV operations without regressions.

- Automated test suite: **226 passed** (`uv run pytest -q`).
- Regression verification suites: **9/9 PASS** (`verify/verify_findings_iter1.py`), **6/6 PASS** (`verify/verify_iter2.py`).
- Adversarial checks (`/tmp/verify_iter2_xlsx.py`): **ALL PASSED**.
  - `init-mapping` outputs clear format-specific metadata for both CSV and XLSX without errors.
  - Sheet selection precedence correctly prioritizes exact sheet names (e.g. numeric names like `"2"`, `"2024"`, `"10"`) before falling back to 1-based sheet index.
  - Column width handling correctly formats multi-column layouts (>26 columns up to "BA"+) using `get_column_letter` without syntax errors.
  - Column widths are properly registered before streaming starts, including with `--no-header`.
  - The `--sheet` flag is accepted symmetrically as an alias in `csmt export`.
  - Pipeline logs utilize `TableInfo.describe()` uniformly across CSV and XLSX.

No blocking, high, or medium severity bugs remain.

---

## Verification of REVIEW-6 Fixes

1. **Fixed (F1 - HIGH): `csmt init-mapping` summary on `.xlsx` files**
   - **Verification**: `cmd_init_mapping` in `csmt/cli.py` inspects `source.info()`. It prints delimiter/encoding for CSV sources and sheet/header-row/column count for XLSX sources, avoiding attribute errors on non-CSV sources.
2. **Fixed (F2 - HIGH): Sheet name vs index precedence in `XlsxSource._select_sheet`**
   - **Verification**: Exact string match in `workbook.sheetnames` is evaluated before `str.isdigit()` index conversion. Sheets named with digits (e.g. `"2024"`, `"2"`) are correctly selected by name, while non-existent numeric names fall back gracefully to 1-based sheet index matching.
3. **Fixed (F3 - MEDIUM): Auto column width registration in `XlsxSink`**
   - **Verification**: `write_header` sets column dimensions before openpyxl emits the first `<cols>` element, using `get_column_letter(index)` for safe column letters ("A" to "ZZZ"). Wide tables (>26 columns) export cleanly without syntax errors or openpyxl `ValueError`.
4. **Fixed (Advisory A1 - LOW): Pipeline start log formatting**
   - **Verification**: `ImportPipeline.run()` logs `info.describe()`, producing clean, format-specific log lines (`format=csv, encoding=utf-8, delimiter=','` and `format=xlsx, sheet='...', columns=...`) instead of `encoding=None, delimiter=None`.
5. **Fixed (Advisory A2 - LOW): `--sheet` alias in `export`**
   - **Verification**: `export_parser` accepts `--sheet` alongside `--sheet-name`, aligning CLI flags across `import`, `init-mapping`, and `export`.

---

## Non-Blocking Advisory Notes

1. **Advisory (LOW / Non-Blocking): Column index lookup in `XlsxSink.write_row`**
   - **Context**: `csmt/export.py:328`
   - **Details**: `index = list(fieldnames).index(name) + 1` performs a linear index lookup whenever a cell's width exceeds the recorded column width. In `write_row`, iterating with `enumerate(fieldnames, start=1)` would provide the column index directly without the list conversion and lookup.
   - **Impact**: Non-blocking; negligible overhead on typical column counts.

2. **Advisory (LOW / Non-Blocking): Openpyxl streaming column width behavior**
   - **Context**: `csmt/export.py:299-308`
   - **Details**: In openpyxl write-only mode (`write_only=True`), worksheet `<cols>` dimensions are permanently written to the output stream upon the first `append()`. Subsequent cell length updates on row 2 onwards do not alter the already emitted `<cols>` XML.
   - **Impact**: Non-blocking; this is an inherent design constraint of streaming OpenXML writers that maintain O(1) memory consumption.
