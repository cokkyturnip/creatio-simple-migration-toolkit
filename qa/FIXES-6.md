# Fixes applied after REVIEW-6 (XLSX round, iteration 1)

All 3 findings confirmed by failing checks first (`/tmp/verify_r6.py`), then fixed, plus
the 2 LOW advisories. Regression tests added in `tests/test_xlsx.py` (section
"REVIEW-6 regressions").

| # | Sev | Temuan | Perbaikan |
|---|-----|--------|-----------|
| 1 | HIGH | `csmt init-mapping --csv data.xlsx` crash `AttributeError: 'XlsxSource' object has no attribute 'delimiter'` | Ringkasan hasil kini memakai `source.info()`: untuk CSV mencetak delimiter/encoding, untuk XLSX mencetak sheet, header row, dan jumlah kolom |
| 2 | HIGH | Sheet bernama angka ("2", "2024") salah ditafsirkan sebagai indeks sheet → data salah atau error | Nama sheet **persis** menang lebih dulu; indeks hanya dipakai bila tidak ada sheet dengan nama itu |
| 3 | MEDIUM | Lebar kolom XLSX tidak pernah tertulis (diubah setelah append, diabaikan openpyxl) dan notasi kolom `A26` invalid untuk >26 kolom | `write_header` mendaftarkan lebar sebelum append pertama memakai `get_column_letter`; `close()` tidak lagi memutasi `<cols>` |
| A1 | LOW | Log pipeline menampilkan `encoding=None, delimiter=None` untuk spreadsheet | Memakai `TableInfo.describe()` |
| A2 | LOW | `export` memakai `--sheet-name` sementara subcommand lain memakai `--sheet` | `--sheet` diterima sebagai alias di `export` |

## Verifikasi

- Skrip cek REVIEW-6: 4/4 PASS (`init-mapping xlsx`, sheet "2", sheet "2024", lebar kolom)
- `uv run pytest -q` → **226 passed** (220 + 6 tes regresi)
- Verifier REVIEW-1 **9/9**, REVIEW-2 **6/6**

## Advisory REVIEW-7 yang ikut dirapikan

- `XlsxSink.write_row` tidak lagi memutasi lebar kolom setelah `append()` (openpyxl
  mode write-only mengabaikannya). Lebar kini hanya ditetapkan di `write_header` dengan
  `get_column_letter`, sehingga tidak ada kode mati maupun pencarian indeks linear.
  `pytest -q` → **226 passed**.
