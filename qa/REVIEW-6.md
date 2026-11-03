# Creatio Simple Migration Toolkit (`csmt`) Code Review - Round 6 (XLSX Support)

## Summary of Verification

- Automated test suite: **220 passed** (`uv run pytest -q`).
- Verification suites: **9/9 PASS** (`verify/verify_findings_iter1.py`), **6/6 PASS** (`verify/verify_iter2.py`).
- Adversarial test suite (`/tmp/verify_r6_adversarial.py`): Header offsets, ragged rows, multiple sheets, 1200+ row streaming, formulas (`data_only`), booleans, ISO dates/times, and XLSX roundtrip export-to-import all verified.
- 3 real findings identified (2 HIGH, 1 MEDIUM) plus 2 non-blocking advisories.

---

## Findings

### 1. HIGH: `csmt init-mapping` crashes on `.xlsx` files with `AttributeError`
- **Location**: `csmt/cli.py:424`
- **Problem**: When generating a starter mapping from an Excel file via `csmt init-mapping --csv data.xlsx`, the command crashes with:
  ```text
  AttributeError: 'XlsxSource' object has no attribute 'delimiter'
  ```
- **Why it matters**: `cmd_init_mapping` was updated with `--sheet` and `--header-row` flags to support `.xlsx` inputs via `open_source()`, but line 424 assumes `source` is always a `CsvSource`:
  ```python
  print(f"Detected delimiter {source.delimiter!r}, encoding {source.encoding}")
  ```
  `XlsxSource` implements the `TabularSource` protocol and does not have `delimiter` or `encoding` attributes. This completely breaks the `init-mapping` command when used with Excel workbooks.
- **Concrete Fix**: Inspect `source.info()` or check the instance type before accessing CSV-specific attributes:
  ```python
  info = source.info()
  if info.format == "csv":
      print(f"Detected delimiter {info.delimiter!r}, encoding {info.encoding}")
  else:
      print(f"Detected sheet {info.sheet!r}, {len(info.fieldnames)} columns")
  ```

---

### 2. HIGH: Numeric sheet names (e.g. "2024") break sheet selection in `XlsxSource`
- **Location**: `csmt/xlsx_source.py:107-115`
- **Problem**: Passing `--sheet 2024` for a worksheet named `"2024"` (e.g. financial year, period, or numeric code) triggers `sheet.strip().isdigit()`, parsing it as a 1-based sheet index instead of an exact sheet name:
  ```python
  names = workbook.sheetnames
  if isinstance(sheet, str) and sheet.strip().isdigit():
      sheet = int(sheet.strip())
  if isinstance(sheet, int):
      if not 1 <= sheet <= len(names):
          raise CsvError(
              f"Sheet index {sheet} is out of range; workbook has {len(names)} "
              f"sheet(s): {', '.join(names)}"
          )
      self._sheet_name = names[sheet - 1]
      return workbook[self._sheet_name]
  ```
- **Why it matters**:
  1. If the numeric name exceeds the number of sheets (e.g., year `"2024"` in a 3-sheet workbook), it crashes with `CsvError: Sheet index 2024 is out of range; workbook has 3 sheet(s): ...`, even though the sheet exists.
  2. If the numeric name falls within range (e.g., a sheet named `"2"` in a workbook with sheets `["Overview", "Summary", "2"]`), `--sheet 2` silently selects index 2 (`"Summary"`) rather than the target sheet `"2"`, causing silent data corruption/mismatched imports.
- **Concrete Fix**: Prioritize exact sheet name matching in `names` before attempting numeric index fallback:
  ```python
  names = workbook.sheetnames
  if sheet in names:
      self._sheet_name = sheet
      return workbook[sheet]

  if isinstance(sheet, str) and sheet.strip().isdigit():
      sheet = int(sheet.strip())
  if isinstance(sheet, int):
      if not 1 <= sheet <= len(names):
          raise CsvError(
              f"Sheet index {sheet} is out of range; workbook has {len(names)} "
              f"sheet(s): {', '.join(names)}"
          )
      self._sheet_name = names[sheet - 1]
      return workbook[self._sheet_name]

  raise CsvError(
      f"Sheet {sheet!r} not found in {self.path.name}; "
      f"available: {', '.join(names)}"
  )
  ```

---

### 3. MEDIUM: Auto column widths in `XlsxSink` are a no-op and contain invalid column syntax for >26 columns
- **Location**: `csmt/export.py:323-328`
- **Problem**: In `XlsxSink`:
  ```python
  def close(self) -> None:
      letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
      for index, (name, width) in enumerate(self._widths.items()):
          column = letters[index % 26] if index < 26 else f"A{index}"
          self._sheet.column_dimensions[column].width = width
      self._workbook.save(self.options.out)
  ```
  1. In openpyxl's `write_only=True` mode, the worksheet header containing `<cols>` is emitted to the XML stream when the first row is appended. Modifying `column_dimensions` inside `close()` (after data rows have already been streamed) is completely ignored by openpyxl; the generated file has no `<cols>` elements and all columns remain at default width.
  2. The column key generation logic `f"A{index}"` is invalid Excel column notation: column 26 becomes `"A26"`, column 27 becomes `"A27"`, etc. (which are cell coordinates rather than "AA", "AB"). If openpyxl were to evaluate them, it raises `ValueError: 'A26' is not a valid column name`.
- **Why it matters**: Exported Excel workbooks do not have formatted column widths as intended, and the indexing logic is syntactically invalid for wide tables.
- **Concrete Fix**: Set column dimensions in `write_header` *before* the first `append()` using `openpyxl.utils.get_column_letter`, and remove the late mutation in `close()`:
  ```python
  from openpyxl.utils import get_column_letter

  def write_header(self, fieldnames: Sequence[str]) -> None:
      for index, name in enumerate(fieldnames):
          col_letter = get_column_letter(index + 1)
          width = max(8, min(len(str(name)) + 2, 60))
          self._sheet.column_dimensions[col_letter].width = width
      if self.options.write_header:
          self._sheet.append(list(fieldnames))
  ```

---

## Non-Blocking Advisory Notes

1. **Advisory (LOW / Non-Blocking): Pipeline log displays `encoding=None, delimiter=None` for spreadsheets**
   - **Context**: `csmt/pipeline.py:283-289`
   - **Details**: `ImportPipeline.run()` logs `Reading {path} (encoding={info.encoding}, delimiter={info.delimiter!r}, {len} columns)`. For spreadsheets, encoding and delimiter are `None`. Calling `info.describe()` (defined in `TableInfo`) would format both CSV and XLSX runs cleanly.

2. **Advisory (LOW / Non-Blocking): CLI option naming for sheets across subcommands**
   - **Context**: `csmt/cli.py:185, 234, 274`
   - **Details**: `import` and `init-mapping` use `--sheet`, while `export` uses `--sheet-name`. Accepting `--sheet` as an alias for `--sheet-name` in `csmt export` would prevent user confusion when exporting to Excel.
