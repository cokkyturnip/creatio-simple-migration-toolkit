# Fixes applied after REVIEW-2 (`agy` reviewer, iteration 2)

All 5 findings addressed and automated-verified (`/tmp/verify_iter2.py` → 6/6 PASS; iter-1
verifier tetap 9/9 PASS; suite penuh **125 passed**).

| # | Sev | Temuan REVIEW-2 | Perbaikan |
|---|-----|-----------------|-----------|
| 1 | BLOCKER | Key lookup mem-query Creatio dengan teks mentah (`CountryId eq Indonesia`) | `PreparedRow` kini punya dua representasi key: `key_values` (string CSV, untuk dedupe/journal/report) dan `key_query_values` (GUID hasil resolve, untuk `ExistingIndex`). Semua akses server (`preload`, `get`, `remember`, reconciliation) memakai `key_query_values`; `finalize()` mengisi lookup-key GUID ke situ, sedangkan `key_values` tetap teks CSV sehingga resume/dedupe tetap stabil |
| 2 | BLOCKER | `_reconcile_failed_create` memanggil `remember(key, None)` sebelum `preload()` → cache berisi None → `preload()` skip query → reconciliation mati total | Ganti `remember(None)` dengan evict `self.index.cache.pop(cache_key, None)` sehingga `preload()` benar-benar menanyakan server. Diverifikasi: batch yang drop response tetap direkreasi dan tidak jadi duplikat |
| 3 | HIGH | Resume per baris fisik tanpa validasi isi → CSV bergeser/berubah antar-run menyebabkan data hilang (skip paksa) / duplikat | Skip resume hanya bila `journal[row].key == dedupe_key` saat ini. Baris baris yang tak cocok (direorder/diedit/insert) di-proses ulang dengan warning sekali; mode `upsert` kemudian memperbaiki diri lewat key match di server |
| 4 | HIGH | Baris yang di-resume tidak didaftarkan di `self._seen` → duplikat di belakangnya lolos duplicate-policy | Baris yang di-skip karena resume kini mendaftarkan `self._seen[dedupe_key] = row.lineno`, jadi baris berikutnya yang punya key sama tetap tertangkap duplicate-policy (`error`/`first_wins`) |
| 5 | MEDIUM | POST/`$batch` tidak di-retry saat 429 (Retry-After diabaikan) | 429 di-retry untuk SEMUA method (rate limit menolak sebelum eksekusi — tidak ada risiko duplikasi); 5xx tetap tidak pernah di-resend untuk POST/batch. Diverifikasi: POST pada 429 baru diulang, POST pada 500 tepat satu percobaan |

## File berubah

- `creatio_importer/mapping.py` — `PreparedRow.key_query_values`; `prepare`/`finalize` mengisi kedua representasi
- `creatio_importer/pipeline.py` — semua jalur ExistingIndex pakai `key_query_values`; evict cache pada reconciliation; resume skip divalidasi key + daftar `_seen`
- `creatio_importer/client.py` — 429 di-retry untuk semua method; 5xx POST/batch tetap fail-once
