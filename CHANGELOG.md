# Changelog

Semua perubahan penting pada proyek ini dicatat di berkas ini.

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/), dan penomoran versi mengikuti [Semantic Versioning](https://semver.org/lang/id/) (`MAJOR.MINOR.PATCH`).

> Versi Python ini berjalan seiring versi Node yang setara ([csmt-nodejs](https://github.com/cokkyturnip/csmt-nodejs)); nama versi tidak dijamin identik. Perbandingan riwayat ditemukan pada "Perbandingan dengan csmt-nodejs".

## [Unreleased]

### Ditetapkan untuk rilis berikutnya (belum dikerjakan)

- Port fitur `template` dari `csmt-nodejs@1.1.0` (template CSV/XLSX dari `$metadata` beserta mapping).

## [1.0.1] - 2026-11-06

### Diperbaiki

- `schema` dan semua pemakai metadata gagal pada Creatio .NET Framework dengan `HTTP 500 An error has occurred.`: `$metadata` diminta memakai `Accept: application/json` dari default `ODataClient.request()`, padahal platform tersebut tidak mendukung metadata JSON. `fetch_metadata()` kini memakai `Accept: application/xml`. Permintaan data tidak berubah; tanpa perbaikan ini 500 juga masuk `RETRY_STATUSES` sehingga kegagalan diulang 5 kali sebelum dilaporkan.
- Diagnosis menyesatkan bila `$metadata` membalas halaman HTML (misalnya path OData salah, diarahkan ke halaman login): XHTML sempat lolos parser XML sehingga error berbunyi `Entity set 'Contact' was not found in $metadata (known: )`. Kini teks yang tampak seperti HTML ditolak sebelum parsing dengan pesan yang menyebut halaman HTML dan saran berganti path `/0/odata` / `/odata`.

### Diubah

- Versi_MINOR: kolom sistem di`--include-system` (opsi dapat digunakan ulang jika Lae butuh).

### Perbandingan dengan csmt-nodejs

| Penyelamatan | csmt-nodejs | Python toolkit ini |
|---|---|---|
| Hint wizard pakai nama paket yang benar | Diperbaiki di `1.0.1` | Sudah benar sejak awal (`uv run csmt check`), tidak perbaikan diperlukan |
| `$metadata` dengan `Accept: application/xml` | Diperbaiki di `1.0.1` | **Diperbaiki di `1.0.1`** |
| Perintah `template` (CSV/XLSX dari `$metadata`) | Rilis di `1.1.0` | Belum tersedia; jalur penerbitan `Unreleased` |
| Nomor versi dibaca otomatis | Diperbaiki di `1.1.0` | Tidak berlaku — versi Python diambil dari `package.json` sendiri (basis sama `pyproject.toml`) |

## [1.0.0] - 2026-11-04

### Ditambahkan

- Rilis publik pertama `creatio-simple-migration-toolkit` dengan perintah `wizard`, `check`, `schema`, `init-mapping`, `import`, dan `export`.
- Impor CSV/XLSX ke Creatio melalui OData 4 dengan mode `create`, `update`, dan `upsert`.
- Jurnal resume per baris fisik (JSONL) untuk melanjutkan impor tanpa mengulang baris yang sukses.
- Pengelompokan `$batch` maksimum 100 sub-request sesuai batasan Creatio.
- Proteksi retry: POST/create tidak pernah dikirim ulang secara membabi-buta pada galat transport atau 5xx/timeout.
- Caching lookup per-run dan negative caching untuk record lookup yang tidak ditemukan.
- Ekspor ke CSV/XLSX dengan filter, `--select`, `--orderby`, dan ekspansi lookup menjadi teks nama.
- Deteksi otomatis format: CSV (UTF-8, UTF-8-BOM, CP1252/Latin-1) dan Excel `.xlsx`/`.xlsm`, termasuk pemilihan sheet dan baris header.
- Jalur audit: laporan akhir CSV, ringkasan, dan jurnal JSONL per baris.
- Autentikasi OAuth 2.0 client credentials, `forms`, dan `basic`, plus perintah `wizard` yang menulis `.env` dengan izin `0600`.

[Unreleased]: https://github.com/cokkyturnip/creatio-csv-import/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/cokkyturnip/creatio-csv-import/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/cokkyturnip/creatio-csv-import/releases/tag/v1.0.0
