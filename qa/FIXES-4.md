# Fixes applied after REVIEW-4 (`agy` reviewer, export round, iteration 1)

All 4 findings confirmed with failing checks first, then fixed. Regression tests added in
`tests/test_export.py` (section "REVIEW-4 regressions").

| # | Sev | Temuan | Perbaikan |
|---|-----|--------|-----------|
| 1 | HIGH | `--limit 0` tetap menulis 1 baris data | Content guard di awal `Exporter.run()`: `limit == 0` hanya menulis header (bila `--select` ada) lalu berhenti tanpa mengambil data. Diverifikasi: 0 baris tertulis dan **tidak ada** request `GET` ke entity |
| 2 | HIGH | Cache lookup case-sensitive: GUID huruf besar di FK tidak cocok dengan GUID huruf kecil dari server → caption gagal dan query berulang tiap flush | Fungsi `id_key()` menormalkan GUID (`strip("{}")` + `casefold()`); dipakai untuk menulis, memeriksa, dan membaca cache |
| 3 | MEDIUM | GUID ber-kurung kurawal (`{...}`) di-quote di `$filter` → Creatio menjawab 400 `Incompatible types 'Edm.Guid' and 'Edm.String'` | `clean_guid()` menghapus kurung/spasi sebelum dicocokkan; `_id_literal()` memakai hasilnya sehingga GUID selalu tanpa kutip, non-GUID tetap di-quote + di-escape |
| 4 | MEDIUM | ID lookup yang tidak ada (terhapus/nonaktif) tidak di-cache → query ulang setiap flush 200 baris | Set `_missing` per (entity, caption) menyimpan ID yang sudah dipastikan tidak ada, sehingga tidak ditanyakan lagi. Sengaja **dipisah** dari cache caption agar GUID asli tetap diekspor (tidak jadi sel kosong) |

Catatan desain: perbaikan #4 semula menulis `""` ke cache caption. Itu membuat data hilang
(GUID asli berubah jadi kosong), sehingga dibatalkan. Sekarang dua struktur terpisah:
`_cache` (caption ditemukan) dan `_missing` (dipastikan tidak ada, GUID dipertahankan).

## Verifikasi

- `uv run pytest -q` → **186 passed** (182 + 4 tes regresi)
- Verifier REVIEW-1 **9/9**, REVIEW-2 **6/6**
- Skrip cek temuan REVIEW-4: 4/4 PASS (`/tmp/verify_r4.py`)

## Advisory dari REVIEW-5 yang ikut diperbaiki

- **`--limit 0` tanpa `--select` menghasilkan file 0 byte.** Sekarang nama kolom diambil
  dari `$metadata` (`entity_fields`) sehingga header tetap tertulis tanpa membaca satu
  record pun. Bila `$metadata` tidak bisa diakses, exporter mencatat warning dan menulis
  file kosong (tidak gagal). Tes: `test_export_limit_zero_without_select_uses_metadata_header`.
  Verifikasi: `pytest -q` → **187 passed**.
