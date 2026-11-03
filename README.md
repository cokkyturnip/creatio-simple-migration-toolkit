# CSMT — Creatio Simple Migration Toolkit

[![CI Tests](https://img.shields.io/badge/tests-244%20passed-brightgreen.svg)](file:///Users/cokkyturnip/creatio-csv-import/tests)
[![Python Version](https://img.shields.io/badge/python-≥%203.11-blue.svg)](file:///Users/cokkyturnip/creatio-csv-import/pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](file:///Users/cokkyturnip/creatio-csv-import/LICENSE)

> **Navigasi Bahasa / Language Navigation:**  
> 🇮🇩 [Bahasa Indonesia](#-bahasa-indonesia) | 🇬🇧 [English](#-english)  
> 📦 **Node.js Version:** Tersedia juga versi Node.js / TypeScript di [github.com/cokkyturnip/csmt-nodejs](https://github.com/cokkyturnip/csmt-nodejs)

---

## 🇮🇩 Bahasa Indonesia

Toolkit CLI mandiri untuk memindahkan data *spreadsheet* (CSV / XLSX) ke Creatio dan sebaliknya melalui antarmuka resmi OData 4.

```text
┌────────────────────────┐                               ┌────────────────────────┐
│   Spreadsheet Lokal    │      ⚡ Impor (Upsert)        │    Creatio Platform    │
│   • CSV (Auto-Delim)   │ ────────────────────────────► │   • OData 4 Service    │
│   • Excel XLSX / XLSM  │ ◄──────────────────────────── │   • 1.800+ Collections │
└────────────────────────┘      📤 Ekspor (+ Lookups)    └────────────────────────┘
            ▲                                                        │
            │           🛡️ Jurnal Resume & Audit Per Baris           │
            └────────────────────────────────────────────────────────┘
```

- ⚡ **Impor Cepat & Aman**: Memuat CSV/Excel ke entitas Creatio (`create`, `update`, `upsert`) dengan validasi skema di awal, auto-batching 100 baris, dan proteksi anti-duplikasi.
- 📤 **Ekspor Kaya Konteks**: Menarik data OData ke CSV/Excel dengan filter kustom dan otomatis menerjemahkan GUID relasi *lookup* menjadi teks nama yang mudah dibaca.
- 🔄 **Resume Bebas Pusing**: Jika jaringan terputus di tengah jalan, lanjutkan secara instan tanpa mengulang baris yang telah sukses berkat pencatatan jurnal per baris fisik.
- 🛡️ **Zero-Trust & Full Audit**: Berjalan 100% di perimeter lokal klien — vendor tidak pernah menyentuh data mentah, dan setiap baris tercatat status serta kode HTTP-nya.

| Informasi | Spesifikasi |
|---|---|
| **Versi** | 1.0.0 |
| **Kebutuhan Runtime** | Python ≥ 3.11 (dikunci pada Python 3.12 via `.python-version`) |
| **Dependensi Inti** | `requests`, `openpyxl` (eksekusi melalui runner cepat `uv`) |
| **Status Pengujian** | 244 tes otomatis lulus · 2 skrip verifikasi lulus · `uv.lock` terkunci |
| **Versi Node.js** | Tersedia di [github.com/cokkyturnip/csmt-nodejs](https://github.com/cokkyturnip/csmt-nodejs) |
| **Lisensi** | MIT |

---

### Mengapa CSMT: Menyelesaikan Masalah Import Bawaan

Banyak proyek Creatio mengandalkan fitur impor berkas bawaan di antarmuka web untuk *bulk load*. Pola umum yang sering terjadi: klien mengekspor data mentah ke berkas, lalu menyerahkan berkas tersebut ke pihak ketiga (vendor atau konsultan) untuk diunggah.

Pola ini memicu dua masalah tata kelola data:
1. **Risiko Kebocoran Data Pribadi**: Sekali berkas diserahkan ke pihak luar, hak akses tidak lagi dapat diaudit atau dicabut. Salinan berkas rawan tersebar melalui email atau media penyimpanan fisik.
2. **Kekhawatiran Egress API yang Berlebihan**: Sering kali terdapat kekhawatiran bahwa integrasi API memakan kuota bandwidth yang masif. Padahal, 100.000 baris × ±400 byte hanya berukuran ~40 MB. Biaya sebenarnya ada pada kegagalan transaksi yang harus diulang dari awal.

Selain tata kelola, fitur impor web bawaan memiliki keterbatasan teknis:
- Tidak ada validasi pra-unggah (data rusak baru terdeteksi di tengah jalan).
- Tidak ada jaminan idempotensi (unggah ulang berisiko menimbulkan data duplikat).
- Tidak dapat melanjutkan (*resume*) proses saat koneksi jaringan terputus.
- Tidak ada berkas audit mendalam per baris.

**Pendekatan CSMT:**
CSMT dijalankan **di dalam perimeter keamanan klien** (laptop internal atau VM privat) dan berbicara langsung ke Creatio melalui OAuth 2.0. Vendor hanya perlu menyerahkan berkas `mapping.json` dan biner tool — **tanpa pernah memegang data aktual klien**. Akses API dapat dicabut sewaktu-waktu dengan me-rotate *client secret*.

> [!NOTE]
> CSMT tidak menghilangkan kebutuhan pengiriman data (payload tetap masuk ke Creatio OData), tetapi memangkas frekuensi request HTTP, menjamin keamanan transaksi, dan meniadakan risiko kebocoran data di luar perimeter.

---

### Fitur Utama & Jaminan Teknis

Setiap jaminan teknis di bawah ini didukung oleh uji regresi otomatis:

1. **Aman Diulang Tanpa Duplikat (`--mode upsert`)**  
   Mencocokkan rekaman berdasarkan kunci primer/unik (mendukung *multi-key* dan relasi *lookup*). Eksekusi ulang pada data yang sama akan melakukan *update*, bukan menduplikasi entitas.
2. **Resume Berbasis Jurnal Fisik**  
   Mencatat kemajuan per baris fisik berkas ke dalam jurnal JSONL beserta verifikasi kunci. Jika baris CSV bergeser atau diedit, baris yang telah sukses tidak akan terlewat atau tertimpa secara keliru.
3. **Pengelompokan Batch Sesuai Batasan Creatio**  
   Membatasi `$batch` tepat maksimum 100 sub-request sesuai batasan resmi Creatio (`Prefer: continue-on-error`). Memproses 100.000 rekaman hanya membutuhkan ±1.000 panggilan HTTP, bukan 100.000 panggilan terpisah.
4. **Proteksi Kegagalan Jaringan (No Blind POST Retry)**  
   Operasi POST / create tidak pernah dikirim ulang secara membabi-buta saat terjadi galat transport atau 5xx/timeout. Client merekonsiliasi keberadaan rekaman di server terlebih dahulu guna mencegah terciptanya entitas ganda.
5. **Lookup Caching & Negative Caching**  
   Mencegah lonjakan kueri berulang untuk nilai lookup yang identik. Jika sebuah nilai lookup tidak ditemukan di Creatio, hasil negatif dicatat dalam cache sesi agar tidak menembak endpoint berkali-kali.
6. **Streaming Paging dengan Konsumsi Memori Konstan O(1)**  
   Membaca dan menulis data per bongkahan (*chunked streaming*), menjaga konsumsi memori RAM tetap datar bahkan saat memproses ratusan ribu baris data.
7. **Deteksi Otomatis & Fleksibilitas Format**  
   Mendukung CSV (otomatis mendeteksi encoding UTF-8/UTF-8-BOM/Latin-1 dan delimiter `,`, `;`, `\t`, `|`) serta spreadsheet Microsoft Excel `.xlsx` / `.xlsm`.
8. **Jalur Audit Lengkap**  
   Menghasilkan laporan akhir CSV, ringkasan JSON, dan jurnal JSONL dengan rincian status per baris, kode HTTP, ID rekaman, dan pesan galat.

---

### Instalasi

CSMT direkomendasikan dijalankan melalui **[uv](https://docs.astral.sh/uv/)**. Anda tidak perlu memasang atau mengelola lingkungan virtual Python secara manual — `uv` akan otomatis mengunduh Python 3.12 secara terisolasi tanpa menyentuh Python bawaan sistem operasi.

#### 1. Pasang uv

| Sistem Operasi | Perintah Instalasi |
|---|---|
| **macOS** | `brew install uv` atau `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Windows (PowerShell)** | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **Windows (winget/scoop)** | `winget install --id=astral-sh.uv -e` atau `scoop install main/uv` |
| **Linux (Ubuntu/Debian)** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Alternatif (Semua OS)** | `pipx install uv` |

#### 2. Kloning Repositori & Verifikasi

```bash
git clone https://github.com/cokkyturnip/creatio-csv-import.git
cd creatio-csv-import

# Menampilkan bantuan CLI (.venv otomatis dibuat pada eksekusi pertama)
uv run csmt --help

# Menjalankan seluruh test suite otomatis (244 tes)
uv run pytest -q
```

---

### Panduan Mulai Cepat (Quickstart)

Alur kerja tipikal migrasi data dari awal hingga selesai:

```bash
# 1. Konfigurasi interaktif (menulis file .env dengan izin aman 0600)
uv run csmt wizard

# 2. Uji koneksi, autentikasi, dan hak akses entitas
uv run csmt check --entity Contact

# 3. Inspeksi skema entitas Creatio dari metadata OData
uv run csmt schema --entity Contact

# 4. Generate kerangka file mapping awal dari header CSV
uv run csmt init-mapping --csv data.csv --entity Contact --key Email --out mapping.json

# 5. Uji simulasi (Dry Run) — memvalidasi pemetaan tanpa menulis ke Creatio
uv run csmt import --csv data.csv --mapping mapping.json --dry-run --validate-fields

# 6. Eksekusi impor aktual (mode upsert dengan pengelompokan batch 50 baris)
uv run csmt import --csv data.csv --mapping mapping.json --mode upsert --batch-size 50

# 7. Jika koneksi terputus di tengah jalan, lanjutkan secara aman:
uv run csmt import --csv data.csv --mapping mapping.json --mode upsert --resume
```

#### Contoh Ekspor Data

```bash
# Ekspor rekaman ke CSV dengan ekspansi GUID lookup menjadi nama teks
uv run csmt export --entity Contact --out contacts.csv \
    --select Name,Email,CountryId --expand-lookups CountryId=Country.Name

# Ekspor rekaman ke Excel XLSX dengan filter tanggal dan pengurutan
uv run csmt export --entity Contact --out contacts.xlsx \
    --filter "CreatedOn ge 2026-01-01" --orderby "CreatedOn desc"
```

---

### Referensi Perintah CLI

| Perintah | Deskripsi |
|---|---|
| `wizard` | Asisten konfigurasi interaktif untuk menghasilkan `.env` (URL, autentikasi, kredensial). |
| `check` | Memverifikasi ketersediaan endpoint OData, validitas token/kredensial, dan izin entitas. |
| `schema` | Menampilkan seluruh field dan tipe data EDM dari entitas berdasarkan `$metadata`. |
| `init-mapping` | Membaca header berkas CSV/XLSX dan menghasilkan kerangka `mapping.json`. |
| `import` | Memproses impor data ke Creatio (`create`, `update`, `upsert`, batch, resume, audit). |
| `export` | Mengunduh rekaman dari Creatio ke format CSV atau XLSX. |

**Standar Kode Keluar (Exit Codes):**
- `0`: Sukses penuh (seluruh baris berhasil diproses).
- `1`: Selesai dengan catatan (terdapat baris yang gagal atau berstatus `unknown`).
- `2`: Kesalahan konfigurasi, argumen CLI tidak valid, atau koneksi ditolak.
- `130`: Proses dibatalkan oleh pengguna via interupsi terminal (`Ctrl+C`).

---

### Variabel Lingkungan & Konfigurasi

Konfigurasi dibaca otomatis dari berkas `.env` pada direktori aktif atau melalui argumen CLI langsung:

| Variabel Lingkungan | Opsi CLI | Keterangan |
|---|---|---|
| `CREATIO_BASE_URL` | `--base-url` | URL dasar instance Creatio (mis. `https://mycompany.creatio.com`). |
| `CREATIO_ODATA_PATH` | `--odata-path` | Path layanan OData (`/0/odata` untuk .NET Framework atau `/odata` untuk .NET Core/8). |
| `CREATIO_AUTH_MODE` | `--auth-mode` | Metode autentikasi: `oauth2` (sangat direkomendasikan), `forms`, atau `basic`. |
| `CREATIO_IDENTITY_URL` | `--identity-url` | Endpoint server identitas untuk OAuth 2.0 (Identity Service). |
| `CREATIO_CLIENT_ID` | `--client-id` | Client ID aplikasi OAuth 2.0. |
| `CREATIO_CLIENT_SECRET` | `--client-secret` | Client Secret aplikasi OAuth 2.0. |
| `CREATIO_USERNAME` | `--username` | Nama pengguna untuk autentikasi mode `forms` / `basic`. |
| `CREATIO_PASSWORD` | `--password` | Kata sandi untuk autentikasi mode `forms` / `basic`. |
| `CREATIO_ENTITY` | `--entity` | Nama default entitas Creatio (mis. `Contact`). |
| `CREATIO_VERIFY_SSL` | `--insecure` | Validasi sertifikat TLS/SSL (default: `true`, set `--insecure` untuk self-signed). |

---

### Spesifikasi Berkas `mapping.json`

Berkas pemetaan mendefinisikan transformasi kolom spreadsheet ke field atribut OData:

```json
{
  "entity": "Contact",
  "key_fields": ["Email"],
  "constants": {
    "ProcessListeners": 0
  },
  "defaults": {
    "Active": true
  },
  "fields": {
    "Nama Lengkap": {
      "target": "Name",
      "required": true,
      "transform": "collapse_spaces"
    },
    "Email": {
      "target": "Email",
      "transform": "lower"
    },
    "Tanggal Lahir": {
      "target": "BirthDate",
      "type": "date",
      "format": "%d/%m/%Y"
    },
    "Negara": {
      "target": "CountryId",
      "type": "lookup",
      "lookup": {
        "entity": "Country",
        "key_field": "Name"
      }
    },
    "Skor": {
      "target": "UsrScore",
      "type": "decimal"
    }
  }
}
```

---
---

## 🇬🇧 English

Self-contained CLI toolkit to migrate spreadsheet (CSV / XLSX) data to and from Creatio through the official OData 4 interface.

```text
┌────────────────────────┐                               ┌────────────────────────┐
│   Local Spreadsheets   │      ⚡ Ingest (Upsert)       │    Creatio Platform    │
│   • CSV (Auto-Detect)  │ ────────────────────────────► │   • OData 4 Service    │
│   • Excel XLSX / XLSM  │ ◄──────────────────────────── │   • 1,800+ Collections │
└────────────────────────┘      📤 Extract (+ Lookups)   └────────────────────────┘
            ▲                                                        │
            │         🛡️ Zero-Data-Leak Resume & Audit Trails        │
            └────────────────────────────────────────────────────────┘
```

- ⚡ **High-Speed Idempotent Import**: Ingests CSV/Excel into Creatio entities (`create`, `update`, `upsert`) with pre-flight type checking, 100-record batching, and zero duplicate creation.
- 📤 **Context-Aware Export**: Extracts OData records to CSV/Excel with dynamic filters and automatic conversion of raw lookup GUIDs into readable text captions.
- 🔄 **Fault-Tolerant Resume**: Recovers instantly from network interruptions without duplicating or skipping records via physical row-level journaling.
- 🛡️ **Zero-Trust Client Perimeter**: Runs 100% locally — OAuth 2.0 credentials stay secure, business data never leaves your environment, and every row produces an audit trail.

| Detail | Specification |
|---|---|
| **Version** | 1.0.0 |
| **Runtime Requirements** | Python ≥ 3.11 (pinned to Python 3.12 via `.python-version`) |
| **Core Dependencies** | `requests`, `openpyxl` (managed seamlessly via `uv`) |
| **Test Verification** | 244 automated unit & integration tests passing · Locked `uv.lock` |
| **Node.js Port** | Available at [github.com/cokkyturnip/csmt-nodejs](https://github.com/cokkyturnip/csmt-nodejs) |
| **License** | MIT |

---

### Why CSMT: Solving the Built-in Import Bottleneck

Many Creatio implementations rely on the web interface's built-in file import tool for bulk migration. In enterprise settings, this frequently leads to an unsafe operational pattern: the client extracts sensitive operational data to a spreadsheet, which is then handed off to external implementation partners or contractors.

This workflow presents critical governance and reliability challenges:
1. **PII and Data Exposure**: Once a spreadsheet leaves client infrastructure, access controls, audit logs, and instant revocation are lost. Copies easily proliferate across untracked emails and external drives.
2. **Exaggerated Egress Concerns**: While direct API pipelines are sometimes feared for consuming bandwidth, 100,000 rows × ±400 bytes is only ~40 MB. The genuine operational cost stems from failed batches requiring complete restarts.

Furthermore, built-in browser imports lack strict transactional guarantees:
- No pre-upload schema validation (corrupted formats fail mid-run).
- No guaranteed idempotency (accidental re-runs introduce duplicate records).
- No fault-tolerant resume mechanism when network interruptions occur.
- No granular, row-by-row audit reporting.

**The CSMT Architecture:**
CSMT executes **entirely inside the client's trusted perimeter** (a secure workstation, private VM, or self-hosted CI runner) communicating directly with Creatio over authenticated OAuth 2.0 endpoints. External vendors supply only the configuration (`mapping.json`) and the toolkit binary — **never the actual business data**. API access can be terminated immediately by rotating the client secret.

> [!NOTE]
> CSMT does not claim to eliminate network transmission (payloads must enter Creatio OData endpoints), but drastically reduces HTTP transaction count, guarantees idempotency, and preserves data perimeter security.

---

### Core Capabilities & Engineering Invariants

Every technical guarantee below is strictly enforced by automated regression suites:

1. **Safe Idempotent Re-runs (`--mode upsert`)**  
   Reconciles records against primary/composite keys (including lookup fields). Re-executing against an existing dataset updates modified records without creating duplicates.
2. **Physical Line & Key Journaling Resume**  
   Tracks physical row progress in a persistent JSONL journal coupled with key verification. Shuffled or edited spreadsheet rows will not cause uncommitted records to be skipped silently.
3. **Strict Creatio-Compliant Batching**  
   Enforces `$batch` payloads to a maximum of 100 sub-requests in accordance with Creatio platform limits (`Prefer: continue-on-error`). Migrating 100,000 rows requires ~1,000 HTTP round-trips rather than 100,000 individual requests.
4. **Transport Fault Protection (No Blind POST Retries)**  
   POST/create operations are never resent blindly upon encountering 5xx errors or network timeouts. The client verifies record existence prior to re-attempting, preventing ghost duplicates.
5. **Lookup & Negative Caching**  
   Caches resolved lookup IDs in-memory. If a lookup value is absent in Creatio, the negative resolution is cached for the duration of the run to prevent repeated redundant queries.
6. **Flat O(1) Memory Streaming**  
   Processes data through chunked streaming iterators, maintaining flat RAM consumption regardless of whether processing 500 or 500,000 records.
7. **Robust Format Detection**  
   Seamlessly parses CSV (auto-detecting delimiters `,`, `;`, `\t`, `|` and encodings UTF-8, UTF-8-BOM, Latin-1) and Microsoft Excel `.xlsx` / `.xlsm` workbooks.
8. **Comprehensive Audit Trails**  
   Emits a per-row CSV report, a summary JSON, and a JSONL journal recording HTTP statuses, Creatio GUIDs, and error diagnostics.

---

### Installation

CSMT is packaged and executed using **[uv](https://docs.astral.sh/uv/)**. You do not need to install Python manually — `uv` provisions an isolated Python 3.12 virtual environment automatically without modifying system packages (PEP 668 compliant).

#### 1. Install uv

| Operating System | Command |
|---|---|
| **macOS** | `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Windows (PowerShell)** | `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **Windows (Package Managers)**| `winget install --id=astral-sh.uv -e` or `scoop install main/uv` |
| **Linux (Ubuntu/Debian)** | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| **Universal Fallback** | `pipx install uv` |

#### 2. Clone Repository & Verify

```bash
git clone https://github.com/cokkyturnip/creatio-csv-import.git
cd creatio-csv-import

# Display CLI help (automatically creates .venv on first run)
uv run csmt --help

# Run all 244 automated test suites
uv run pytest -q
```

---

### Quickstart Guide

A typical end-to-end data import workflow:

```bash
# 1. Interactive configuration wizard (creates .env with 0600 file permissions)
uv run csmt wizard

# 2. Check connection, credentials, and entity access permissions
uv run csmt check --entity Contact

# 3. Inspect target entity EDM schema directly from OData $metadata
uv run csmt schema --entity Contact

# 4. Generate an initial mapping template from source file headers
uv run csmt init-mapping --csv data.csv --entity Contact --key Email --out mapping.json

# 5. Dry-run simulation — validates mapping and data types without writing to Creatio
uv run csmt import --csv data.csv --mapping mapping.json --dry-run --validate-fields

# 6. Execute actual import in upsert mode with batch chunks of 50 records
uv run csmt import --csv data.csv --mapping mapping.json --mode upsert --batch-size 50

# 7. If interrupted, resume seamlessly without duplicating processed rows
uv run csmt import --csv data.csv --mapping mapping.json --mode upsert --resume
```

#### Export Examples

```bash
# Export records to CSV, resolving CountryId lookup GUIDs to Country names
uv run csmt export --entity Contact --out contacts.csv \
    --select Name,Email,CountryId --expand-lookups CountryId=Country.Name

# Export records to Excel XLSX with custom OData filters and ordering
uv run csmt export --entity Contact --out contacts.xlsx \
    --filter "CreatedOn ge 2026-01-01" --orderby "CreatedOn desc"
```

---

### CLI Command Reference

| Command | Purpose |
|---|---|
| `wizard` | Interactive setup to configure credentials and generate `.env`. |
| `check` | Verifies OData endpoint availability, authentication, and entity read access. |
| `schema` | Inspects and lists all available entity attributes and EDM data types. |
| `init-mapping` | Scaffolds a starter `mapping.json` from spreadsheet headers. |
| `import` | Executes migration pipeline (`create`/`update`/`upsert`, batch, resume, audit). |
| `export` | Extracts Creatio records to CSV or Excel with lookup resolution. |

**Process Exit Codes:**
- `0`: Complete success (all records processed without failure).
- `1`: Completed with errors (one or more records failed or ended with `unknown` status).
- `2`: Configuration error, invalid CLI flags, or network connection failure.
- `130`: Interrupted by user signal (`Ctrl+C`).

---

### Environment Variables & Settings

Settings are loaded automatically from a `.env` file in the working directory or passed via CLI flags:

| Environment Variable | CLI Flag | Description |
|---|---|---|
| `CREATIO_BASE_URL` | `--base-url` | Creatio instance root URL (e.g. `https://mycompany.creatio.com`). |
| `CREATIO_ODATA_PATH` | `--odata-path` | Service endpoint (`/0/odata` for .NET Framework or `/odata` for .NET 8). |
| `CREATIO_AUTH_MODE` | `--auth-mode` | Authentication strategy: `oauth2` (recommended), `forms`, or `basic`. |
| `CREATIO_IDENTITY_URL` | `--identity-url` | Identity Server endpoint URL for OAuth 2.0 authentication. |
| `CREATIO_CLIENT_ID` | `--client-id` | OAuth 2.0 application client identifier. |
| `CREATIO_CLIENT_SECRET` | `--client-secret` | OAuth 2.0 application client secret. |
| `CREATIO_USERNAME` | `--username` | Account username for `forms` / `basic` authentication. |
| `CREATIO_PASSWORD` | `--password` | Account password for `forms` / `basic` authentication. |
| `CREATIO_ENTITY` | `--entity` | Default entity set name (e.g. `Contact`). |
| `CREATIO_VERIFY_SSL` | `--insecure` | Verify TLS certificates (default `true`; use `--insecure` for self-signed). |

---

### Mapping Specification (`mapping.json`)

The mapping schema configures field normalization, conversions, and lookup linkages:

```json
{
  "entity": "Contact",
  "key_fields": ["Email"],
  "constants": {
    "ProcessListeners": 0
  },
  "defaults": {
    "Active": true
  },
  "fields": {
    "Full Name": {
      "target": "Name",
      "required": true,
      "transform": "collapse_spaces"
    },
    "Email": {
      "target": "Email",
      "transform": "lower"
    },
    "Birth Date": {
      "target": "BirthDate",
      "type": "date",
      "format": "%d/%m/%Y"
    },
    "Country": {
      "target": "CountryId",
      "type": "lookup",
      "lookup": {
        "entity": "Country",
        "key_field": "Name"
      }
    },
    "Score": {
      "target": "UsrScore",
      "type": "decimal"
    }
  }
}
```

---

### Documentation & Additional Guides

- [User Guide & Advanced Examples](file:///Users/cokkyturnip/creatio-csv-import/docs/PANDUAN-PEMAKAIAN.md) — In-depth usage walkthrough, complex mapping rules, and edge-case handling.
- [Authentication Specification](file:///Users/cokkyturnip/creatio-csv-import/docs/AUTH.md) — Comprehensive OAuth 2.0, Identity Server, Forms, and CSRF token mechanics.
- [Node.js Port](https://github.com/cokkyturnip/csmt-nodejs) — TypeScript / Node.js alternative implementation of the CSMT toolkit.
