# Changelog

Semua perubahan penting pada proyek ini dicatat di berkas ini.

Format mengikuti [Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/), dan penomoran versi mengikuti [Semantic Versioning](https://semver.org/lang/id/) (`MAJOR.MINOR.PATCH`).

> Versi Python ini berjalan seiring versi Node yang setara ([csmt-nodejs](https://github.com/cokkyturnip/csmt-nodejs)); nama versi tidak dijamin identik. Perbandingan riwayat ditemukan pada "Perbandingan dengan csmt-nodejs".

## [Unreleased]

_Belum ada perubahan setelah 1.1.0._

## [1.1.0] - 2026-11-06

### Ditambahkan

- Perintah `template`: menghasilkan template impor CSV atau XLSX langsung dari `$metadata`, konsisten dengan `csmt-nodejs@1.1.0`. Opsi: `--with-mapping` (menulis `mapping.json` sekaligus), `--sample` (satu baris contoh), `--key`, `--include-system`, `--lookup-key`, `--delimiter`, `--no-bom`. Format keluaran ditentukan dari ekstensi `--out` (`.csv`/`.xlsx`).
- Kolom lookup memakai nama navigasi tanpa akhiran `Id` (`Owner`, `Account`, `Type`) dan diisi label/nama rekaman; mapping yang dihasilkan memakai `lookup.key` (default `Name`).
- Output XLSX berisi dua sheet: data (header + baris contoh opsional) dan `schema` (`header`, `target`, `edm_type`, `nullable`, `max_length`, `lookup_entity`).
- `parse_csdl_field_details()` dan `ODataClient.entity_field_details()`: membaca `Nullable`, `MaxLength`, `Key`, dan `NavigationProperty` dari metadata sehingga lookup dikenali tanpa konfigurasi manual.
- 19 tes baru untuk template dan parsing CSDL (total 265).

### Diubah

- Nama paket distribusi menjadi `csmt` (sebelumnya `creatio-simple-migration-toolkit`) agar konsisten dengan nama perintah di kedua implementasi. Nama modul impor tetap `csmt`; entry point tetap `csmt = csmt.cli:main`.
- README: bagian "Jalankan Tanpa Kloning" via `uvx csmt` (setara `npx`), catatan `pipx install csmt`, dan bagian perintah `template` pada dua bahasa.
- `parse_csdl_entity_fields()` kini diturunkan dari `parse_csdl_field_details()`; API dan bentuk hasilnya tidak berubah.

## [1.0.1] - 2026-11-06

### Diperbaiki

- `schema` dan semua pemakai metadata gagal pada Creatio .NET Framework dengan `HTTP 500 An error has occurred.`: `$metadata` diminta memakai `Accept: application/json` dari default `ODataClient.request()`, padahal platform tersebut tidak mendukung metadata JSON. `fetch_metadata()` kini memakai `Accept: application/xml`. Permintaan data tidak berubah; tanpa perbaikan ini 500 juga masuk `RETRY_STATUSES` sehingga kegagalan diulang 5 kali sebelum dilaporkan.
- Diagnosis menyesatkan bila `$metadata` membalas halaman HTML (misalnya path OData salah, diarahkan ke halaman login): XHTML sempat lolos parser XML sehingga error berbunyi `Entity set 'Contact' was not found in $metadata (known: )`. Kini teks yang tampak seperti HTML ditolak sebelum parsing dengan pesan yang menyebut halaman HTML dan saran berganti path `/0/odata` / `/odata`.

### Diubah

- Versi_MINOR: kolom sistem di`--include-system` (opsi dapat digunakan ulang jika Lae butuh).

### Perbandingan dengan csmt-nodejs

| Penyelamatan | csmt-nodejs | Python toolkit ini |
|---|---|---|
| Hint wizard pakai nama paket yang benar | Diperbaiki di `1.0.1` | Sudah benar sejak awal (`uv run csmt check`), tidak perlu perbaikan |
| `$metadata` dengan `Accept: application/xml` | Diperbaiki di `1.0.1` | **Diperbaiki di `1.0.1`** |
| Perintah `template` (CSV/XLSX dari `$metadata`) | Rilis di `1.1.0` | **Rilis di `1.1.0`** |
| Nomor versi dibaca otomatis | Diperbaiki di `1.1.0` | Tidak berlaku — versi Python diambil dari `__init__.py` (harus sinkron dengan `pyproject.toml`) |

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

[Unreleased]: https://github.com/cokkyturnip/creatio-csv-import/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/cokkyturnip/creatio-csv-import/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/cokkyturnip/creatio-csv-import/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/cokkyturnip/creatio-csv-import/releases/tag/v1.0.0
