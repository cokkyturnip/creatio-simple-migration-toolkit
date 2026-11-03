# Fixes applied after REVIEW-1 (`agy` reviewer)

All 9 findings addressed; each verified by an automated check (`/tmp/verify_findings.py` → 9/9 PASS)
plus the full suite: **125 passed** (`pytest -q`).

| # | Sev | Temuan | Perbaikan |
|---|-----|--------|-----------|
| 1 | BLOCKER | `_recover_from_batch_error` mengklaim update sukses tanpa PATCH, dan create lama diklaim sukses hanya karena key sudah ada | Update selalu dieksekusi ulang (`PATCH idempotent`) dengan id terkini; `create` hanya diklaim `created` bila baris ini memang berusaha create **dan** key diketahui belum ada sebelum batch (snapshot pre-state `window_known_absent` + `_created_now`); kalau tidak, status jujur `unknown` dengan HTTP status asli |
| 2 | HIGH | `--resume` pecah saat key field berupa lookup (journal menyimpan GUID, run baru menghitung key dari string CSV) | Resume diindeks per **nomor baris fisik** (`Journal.load_completed` → `{row_lineno: entry}`), bukan per dedupe-key. Entri terakhir per baris menang, jadi kegagalan ulangan meng-void sukses lama (juga nomer #3) |
| 3 | HIGH | Duplikat key: sukses baris 1 menyembunyikan kegagalan baris 2 saat resume | Diselesaikan bersama #2 (indeks per baris fisik) |
| 4 | HIGH | Key field lookup men-quote GUID di `$filter` → `Incompatible types 'Edm.Guid' and 'Edm.String'` | `quote_fields` sekarang `spec.type not in {"guid", "lookup"}` |
| 5 | HIGH | POST/batch di-retry otomatis saat timeout/5xx → risiko duplikat riil | `IDEMPOTENT_METHODS = {GET, PATCH, DELETE}` — POST/`$batch` **tidak pernah** di-resend pada transport error atau 5xx/429. Sebagai gantinya pipeline merekonsiliasi lewat key (`_reconcile_failed_create`): kalau key diketahui absent sebelum POST dan kini ada → `created` (faktual); kalau tidak bisa dipastikan → `unknown` dengan HTTP status asli. Test lama yang mengharuskan retry POST diubah sesuai perilaku baru |
| 6 | MEDIUM | Error server saat lookup query masuk cache negatif → baris selanjutnya pasti gagal | `ODataError` tidak lagi di-swallow: `lookup_cache` hanya diisi dari hasil query yang selesai; `PendingLookup` membawa `error_status/error_message`, `Mapper.finalize` melapor `failed` dengan status HTTP asli (bukan "not found"), dan baris berikutnya tetap mencoba ulang. Berikutnya: baris daring berhasil normal → record dibuat 1/2 |
| 7 | MEDIUM | `normalize_header(["Name","Name_2","Name"])` menghasilkan dua kolom `Name_2` → data hancur | Suffix dipilih dengan loop unik → `[Name, Name_2, Name_3]` |
| 8 | MEDIUM | `$metadata` parser abaikan `BaseType` → `--validate-fields` salah Tolak field inherited (`Id`, `CreatedOn`...) | Walk `BaseType` rekursif (depth-guard 10, anti-cyle) mengumpulkan properti inherited |
| 9 | LOW | `default` pada field lookup dikirim verbatim (HTTP 400) | Default lookup kini masuk alur `PendingLookup` dan di-resolve seperti nilai CSV biasa |

## File berubah

- `creatio_importer/pipeline.py` — recovery/reconciliation baru, snapshot pre-state, quote_fields, lookup error no-cache
- `creatio_importer/client.py` — `IDEMPOTENT_METHODS`, tidak ada retry untuk POST/$batch, CSDL BaseType walk
- `creatio_importer/mapping.py` — key dedupe tetap berbentuk CSV raw, default lookup → pending, error_pending message
- `creatio_importer/journal.py` — `load_completed` per baris fisik; gagal meng-void sukses
- `creatio_importer/csv_source.py` — header dedup unik
- `tests/test_client.py`, `tests/test_pipeline.py` — 2 test disesuaikan perubahan perilaku retry POST (+1 test GET retry)
