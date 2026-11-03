# Panduan Pemakaian: CSMT (Creatio Simple Migration Toolkit)

Panduan ini menjelaskan cara menyiapkan konfigurasi (environment), menyiapkan file CSV
dan mapping, menjalankan **impor** ke Creatio, dan menjalankan **ekspor** dari Creatio,
semuanya melalui OData 4. Semua perintah memakai CLI `csmt`.

Dokumen pendukung:

- [`AUTH.md`](AUTH.md): URL dan alur autentikasi (OAuth 2.0, Forms, Basic)
- [`../README.md`](../README.md): ringkasan fitur dan arsitektur

---

## 1. Ringkasan

`csmt` membaca file CSV, mengubah setiap baris menjadi data entity
Creatio (mis. `Contact`), lalu membuat atau memperbarui record melalui OData 4.

Alur kerja yang disarankan:

```
1. Setup env          → wizard atau file .env
2. check              → pastikan URL, kredensial, dan entity bisa diakses
3. schema             → lihat field apa saja yang tersedia di entity
4. init-mapping       → buat kerangka mapping dari header CSV, lalu edit
5. import --dry-run   → simulasi, tidak menulis apa pun
6. import             → impor sebenarnya, hasilnya tercatat di report
7. import --resume    → jika terputus, lanjutkan tanpa mengulang baris yang sudah sukses
8. export             → tarik data dari Creatio ke CSV (opsional)
```

---

## 2. Prasyarat

| Kebutuhan | Keterangan |
|---|---|
| Python | Minimal 3.11. Tidak perlu dipasang manual: uv mengunduh versi yang dikunci di `.python-version` (3.12) |
| [uv](https://docs.astral.sh/uv/) | Satu-satunya alat yang perlu dipasang. Pasang dengan `brew install uv` |
| Akses jaringan ke Creatio | HTTPS ke site Creatio dan ke identity service (untuk OAuth 2.0) |
| Akun integrasi di Creatio | User khusus untuk integrasi, atau client credentials OAuth 2.0. Lihat [bagian 4.5](#45-autentikasi) |

---

## 3. Instalasi

Satu-satunya yang perlu dipasang adalah **uv**. Python tidak perlu dipasang manual:
uv mengunduh versi yang dikunci di `.python-version` (3.12), tanpa perlu akses root dan
tanpa menyentuh Python sistem. Ini juga menghindari masalah *externally-managed
environment* (PEP 668) di Ubuntu 24.04+, Debian 12+, dan Fedora 40+.

### 3.1 macOS

```bash
brew install uv
```

Alternatif tanpa Homebrew:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 3.2 Windows

PowerShell (tidak perlu administrator):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Atau lewat manajer paket:

```powershell
winget install --id=astral-sh.uv -e
# atau
scoop install main/uv
```

Catatan Windows:

- Binary dipasang di `%USERPROFILE%\.local\bin` dan installer menambahkan folder itu ke
  `PATH`. **Tutup lalu buka kembali terminal** agar `uv` dikenali.
- Perintah `curl ... | sh` di atas khusus macOS/Linux. Di Windows gunakan varian PowerShell.
- Jika `uv` tidak dikenali setelah instalasi, periksa `PATH`:

  ```powershell
  $env:Path -split ';' | Select-String 'local\bin'
  ```

- PowerShell versi lama bisa menolak skrip. Opsi `-ExecutionPolicy ByPass` pada perintah di
  atas hanya berlaku untuk proses itu, tidak mengubah policy sistem.

### 3.3 Linux (Ubuntu, Debian, RHEL, Fedora, Rocky, AlmaLinux)

uv **tidak** menyediakan repositori resmi `apt`/`dnf`. Cara resmi yang didukung adalah
*standalone installer* (tidak perlu root, dipasang ke `~/.local/bin`) atau `pipx`.

**Prasyarat minimal** (umumnya sudah ada, tapi sering belum di image server minimal):

```bash
# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y curl ca-certificates

# RHEL 8/9, Fedora, Rocky Linux, AlmaLinux
sudo dnf install -y curl ca-certificates
```

**Instalasi** (sama untuk semua distro di atas):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Jika `curl` tidak tersedia:

```bash
wget -qO- https://astral.sh/uv/install.sh | sh
```

Lalu muat ulang `PATH` (installer biasanya sudah menambahkannya ke `~/.bashrc` atau
`~/.zshrc`):

```bash
source "$HOME/.local/bin/env"   # atau buka terminal baru
uv --version
```

Alternatif bila organisasi mewajibkan instalasi lewat manajer paket Python:

```bash
sudo dnf install -y pipx        # RHEL/Fedora
sudo apt-get install -y pipx    # Ubuntu/Debian (butuh universe)
pipx ensurepath
pipx install uv
```

Catatan Linux:

- **Tanpa root.** Installer menaruh binary di `~/.local/bin`. Untuk instalasi sistem-wide
  (mis. di image container), set `UV_INSTALL_DIR=/usr/local/bin` sebelum menjalankan
  installer.
- **Distro minimal** kadang tidak punya `ca-certificates`, sehingga unduhan TLS gagal.
  Pasang paket itu lebih dulu (perintah di atas).
- **Arm64/aarch64** (mis. server Graviton, Ampere) didukung binary resmi, tidak perlu
  kompilasi Rust.
- **`uv self update`** hanya berlaku untuk instalasi lewat installer/Homebrew. Bila memakai
  `pipx`/`pip`, perbarui lewat manajer tersebut (`pipx upgrade uv`).
- **Docker/CI:** tersedia image resmi `ghcr.io/astral-sh/uv`.

### 3.4 Verifikasi instalasi

```bash
uv --version                 # versi uv, mis. 0.11.x
cd creatio-csv-import
uv run csmt --version        # menyiapkan .venv otomatis pada pemakaian pertama
uv run pytest -q             # opsional: memastikan lingkungan sehat
```

### 3.5 Lingkungan korporat, proxy, dan offline

Sering relevan di jaringan perusahaan (mis. di balik proxy atau TLS interception):

| Kebutuhan | Setelan |
|---|---|
| Proxy HTTP/HTTPS | `export HTTPS_PROXY=http://proxy:8080`, `HTTP_PROXY=...`, `NO_PROXY=localhost,127.0.0.1` (Windows: `$env:HTTPS_PROXY=...`) |
| Sertifikat internal (TLS interception) | `UV_SYSTEM_CERTS=true` agar uv memakai trust store sistem |
| Mirror Python | `UV_PYTHON_INSTALL_MIRROR=https://mirror/contoh/python-build-standalone` |
| Index paket internal (Nexus/Artifactory) | `UV_DEFAULT_INDEX=https://nexus/repository/pypi/simple` |
| Larang unduh Python (pakai Python sistem) | `UV_PYTHON_DOWNLOADS=never` |
| Lokasi cache | `UV_CACHE_DIR=/var/cache/uv` |
| Tanpa jaringan (air-gapped) | Unduh binary dari GitHub Releases, atau pakai image `ghcr.io/astral-sh/uv`; lalu `UV_OFFLINE=1` saat `uv sync` bila artefak sudah di mirror |

Contoh lengkap untuk server di balik proxy:

```bash
export HTTPS_PROXY=http://proxy.perusahaan:8080
export NO_PROXY=localhost,127.0.0.1
export UV_SYSTEM_CERTS=true
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

### 3.6 Setelah uv terpasang

```bash
cd creatio-csv-import
uv run csmt --help
```

Perintah pertama dengan `uv run` otomatis membuat `.venv` dan memasang dependensi dari
`uv.lock`. Tidak ada langkah instalasi lain.

| File | Fungsi | Di git? |
|---|---|---|
| `pyproject.toml` | Daftar dependensi | Ya |
| `uv.lock` | Versi pasti tiap library, supaya semua mesin sama | Ya |
| `.python-version` | Versi Python yang dipakai | Ya |
| `.env` | Kredensial dan URL Creatio | **Tidak** (ada di `.gitignore`) |
| `.venv/` | Hasil instalasi lokal, dibuat otomatis | Tidak |

Untuk mesin produksi yang tidak perlu alat pengembangan:

```bash
uv sync --no-dev
```

---

## 4. Setting environment

Ada dua cara. Yang pertama direkomendasikan.

### 4.1 Cara 1 (disarankan): wizard interaktif

```bash
uv run csmt wizard
```

Wizard menanyakan secara berurutan. Setiap pilihan bernomor disertai satu baris
penjelasan, dan opsi default ditandai `(default)`. Tekan Enter untuk memakai default.

> **Input tersembunyi.** Password dan client secret tidak ditampilkan saat diketik
> (tidak ada tanda bintang). Wizard memberi tahu hal ini lebih dulu dan menyarankan
> **copy-paste** dari password manager/Creatio, bukan mengetik manual — salah ketik pada
> input tersembunyi tidak terlihat dan sulit dilacak.

| Langkah | Pertanyaan | Keterangan |
|---|---|---|
| 1 | Creatio base URL | Mis. `https://pegadaian.creatio.com`. URL yang berakhiran `/0/odata` juga diterima dan otomatis dirapikan |
| 2 | Runtime | `1` = **.NET Framework → `/0/odata` (default)**, `2` = .NET 8 → `/odata`. Setiap opsi disertai penjelasan di layar. Path kustom (mis. `/1/odata`) diisi manual lewat `CREATIO_ODATA_PATH` |
| 3 | Autentikasi | `1` = **OAuth 2.0 (default, direkomendasikan Creatio)**, `2` = Forms, `3` = Basic. Masing-masing dijelaskan beserta konsekuensinya |
| 3a | Username & password | Hanya untuk Forms/Basic. Password **tidak tampil** saat diketik; wizard menyarankan copy-paste |
| 3b | Client ID & client secret | Hanya untuk OAuth 2.0. Secret **tidak tampil** saat diketik; wizard menyarankan copy-paste |
| 3c | Identity service URL | Hanya untuk OAuth 2.0. Default site cloud: `https://<subdomain>-is.creatio.com` (hyphen). Wizard memakai client ID/secret yang baru diisi untuk memanggil endpoint token: `200` = host+kredensial valid, `400/401` JSON = host benar tetapi kredensial ditolak, TLS/`404` = host salah |
| 4 | Entity default | Mis. `Contact`. Boleh dikosongkan |
| 5 | Verifikasi TLS | `1` = **tidak (default)**, `2` = ya. Default tidak dipilih karena instance internal sering memakai sertifikat self-signed; untuk produksi sebaiknya pilih `2` |
| 6 | Test koneksi sekarang | `1` = ya. Jika gagal, config tetap disimpan dan penyebabnya ditampilkan |

Hasilnya adalah file `.env` di folder kerja dengan permission `600` (hanya owner yang
bisa membaca). Jika `.env` sudah ada, file lama dipindahkan ke `.env.bak` setelah Anda
mengonfirmasi penimpaan.

Wizard juga bisa menulis ke lokasi lain:

```bash
uv run csmt wizard --out config/pegadaian.env
```

### 4.2 Cara 2: edit `.env` manual

```bash
cp .env.example .env
```

Lalu isi nilainya. Contoh untuk OAuth 2.0 di site cloud:

```env
CREATIO_BASE_URL=https://pegadaian.creatio.com
CREATIO_ODATA_PATH=/odata
CREATIO_AUTH_MODE=oauth2
CREATIO_CLIENT_ID=isi-client-id
CREATIO_CLIENT_SECRET=isi-client-secret
CREATIO_IDENTITY_URL=https://pegadaian-is.creatio.com
CREATIO_ENTITY=Contact
CREATIO_VERIFY_SSL=true
```

Contoh untuk Forms (user integrasi, bukan Supervisor):

```env
CREATIO_BASE_URL=https://pegadaian.creatio.com
CREATIO_ODATA_PATH=/odata
CREATIO_AUTH_MODE=forms
CREATIO_USERNAME=integration.user
CREATIO_PASSWORD=isi-password
CREATIO_ENTITY=Contact
CREATIO_VERIFY_SSL=true
```

### 4.3 Daftar variabel

| Variabel | Flag CLI | Wajib | Keterangan |
|---|---|---|---|
| `CREATIO_BASE_URL` | `--base-url` | Ya | URL site, mis. `https://pegadaian.creatio.com` |
| `CREATIO_ODATA_PATH` | `--odata-path` | Tidak | `/odata` untuk .NET 8, `/0/odata` untuk .NET Framework. Default `/0/odata` bila tidak diisi |
| `CREATIO_AUTH_MODE` | `--auth-mode` | Tidak | `oauth2`, `forms`, `basic`, atau `auto` (default). `auto` memilih OAuth 2.0 bila client ID dan secret tersedia, selain itu Forms |
| `CREATIO_CLIENT_ID` | `--client-id` | Untuk OAuth 2.0 | Client ID dari konfigurasi OAuth di Creatio |
| `CREATIO_CLIENT_SECRET` | `--client-secret` | Untuk OAuth 2.0 | Client secret. Jangan dibagikan |
| `CREATIO_IDENTITY_URL` | `--identity-url` | Untuk OAuth 2.0: nilai system setting `OAuth20IdentityServerUrl`. Default site `*.creatio.com` = `https://<subdomain>-is.creatio.com` (hyphen; bentuk `_is` dicoba sebagai cadangan). Token endpoint = `{identity-url}/connect/token` |
| `CREATIO_USERNAME` | `--username` | Untuk Forms/Basic | User integrasi |
| `CREATIO_PASSWORD` | `--password` | Untuk Forms/Basic | Password user integrasi |
| `CREATIO_ENTITY` | `--entity` | Tidak | Entity set default, mis. `Contact`. Bisa ditimpa dengan `--entity` |
| `CREATIO_VERIFY_SSL` | `--insecure` | Tidak | `true` (default). `false` hanya untuk development |
| `CREATIO_TIMEOUT` | `--timeout` | Tidak | Batas waktu HTTP dalam detik. Default 60 |
| `CREATIO_MAX_RETRIES` | `--max-retries` | Tidak | Jumlah percobaan ulang untuk GET/PATCH/DELETE dan 429. Default 5 |
| `CREATIO_RETRY_BACKOFF` | | Tidak | Jeda awal retry dalam detik. Default 1 |
| `CREATIO_RETRY_MAX_DELAY` | | Tidak | Jeda retry maksimum dalam detik. Default 30 |

### 4.4 Urutan prioritas nilai

Jika satu nilai ditulis di beberapa tempat, yang dipakai adalah yang paling atas:

1. Flag CLI (mis. `--base-url`)
2. Environment variable di shell (mis. `export CREATIO_BASE_URL=...`)
3. File `.env` (dari `--env-file <path>`, atau `./.env` di folder kerja)

Gunakan `--env-file` bila konfigurasi tidak berada di folder kerja:

```bash
uv run csmt check --env-file config/pegadaian.env
```

### 4.5 Autentikasi

Ringkasan pilihan (detail URL dan alur ada di [`AUTH.md`](AUTH.md)):

| Metode | `CREATIO_AUTH_MODE` | Keterangan |
|---|---|---|
| OAuth 2.0 client credentials | `oauth2` | Direkomendasikan Creatio untuk integrasi. Butuh client ID, client secret, dan identity service URL |
| Forms | `forms` | Login ke `AuthService.svc`, lalu cookie `BPMCSRF` dikirim di setiap request |
| Basic | `basic` | HTTP Basic. Hanya bekerja bila perlindungan CSRF dimatikan di server |

> **Keamanan.** Gunakan user integrasi khusus dengan hak akses seminimal mungkin.
> Jangan memakai akun Supervisor, dan jangan memberi izin *Can manage configuration
> elements* (`CanManageSolution`) kepada user integrasi.

**Identity service URL (OAuth 2.0).** Nilainya adalah system setting
`OAuth20IdentityServerUrl` (Setup → System settings). Untuk site cloud
`https://<subdomain>.creatio.com`, defaultnya `https://<subdomain>-is.creatio.com`
(**hyphen**). Contoh: `pgdn-6-2026-demo-demo.creatio.com` menjadi
`https://pgdn-6-2026-demo-demo-is.creatio.com`.

Bentuk underscore (`<subdomain>_is.creatio.com`) **bukan** default dan sering menunjuk
host yang tidak ada. Gejalanya `SSL: CERTIFICATE_VERIFY_FAILED ... Hostname mismatch`
atau `404`. csmt mencoba kedua bentuk berurutan (nilai eksplisit tetap dicoba lebih dulu)
dan membedakan *host salah* dari *kredensial salah*.

Untuk domain selain `*.creatio.com` tidak ada default, jadi nilainya wajib diisi manual.

Cara cepat menguji satu kandidat:

```bash
curl -sS -X POST https://<kandidat>/connect/token \
    -d "grant_type=client_credentials&client_id=probe&client_secret=probe"
# {"error":"invalid_client"}  -> host benar
# 404 / TLS mismatch          -> host salah
```

**Path OData (`CREATIO_ODATA_PATH`).** Nilainya **tidak bisa ditebak** dari generasi
runtime. Sebagian instance melayani `/0/odata`, sebagian lain `/odata`; pada tenant yang
sama, `/0/odata` bisa benar sementara `/odata` justru me-redirect ke halaman login.

| Path | Contoh URL | Catatan |
|---|---|---|
| `/0/odata` | `https://host.creatio.com/0/odata` | Umum pada instance Creatio lama; angka `0` adalah nomor workspace |
| `/odata` | `https://host.creatio.com/odata` | Umum pada instance .NET 8 yang baru |

Wizard memakai **`/0/odata` sebagai default** karena paling umum. Cara menentukan
dengan pasti:

1. **Wizard** memeriksa path yang dipilih dengan kredensial asli. Jika `/odata`
   me-redirect ke login atau 404, wizard mencoba `/0/odata` dan otomatis memakainya.
2. Manual: `csmt check --odata-path /0/odata`, lalu ulangi dengan `--odata-path /odata`.
   Pilih yang mengembalikan JSON, bukan HTML.

Gejala path salah adalah respons **HTML halaman login** (bukan selalu 404). csmt
mendeteksinya dan menyarankan path alternatif.

### 4.6 Verifikasi konfigurasi

```bash
uv run csmt check --entity Contact
```

Contoh keluaran yang berhasil (angka dan URL bersifat ilustrasi):

```
{
  "root_url": "https://pegadaian.creatio.com",
  "odata_url": "https://pegadaian.creatio.com/odata",
  "auth_mode": "oauth2",
  "collections": 120,
  "entity": "Contact",
  "entity_reachable": true
}
OK: connection and credentials are valid
```

`check` hanya membaca data dan tidak menulis apa pun. Jalankan perintah ini setiap kali
mengganti konfigurasi.

---

## 5. Menyiapkan data

### 5.1 Format file masukan (CSV atau Excel)

`import` dan `init-mapping` menerima **CSV** dan **Excel `.xlsx`/`.xlsm`**. Format
ditentukan otomatis dari ekstensi file.

| Aspek | CSV | Excel (.xlsx) |
|---|---|---|
| Pemilihan sheet | – | `--sheet NAMA` atau `--sheet 2` (default: sheet pertama) |
| Baris header | baris pertama | `--header-row N` (default 1), berguna bila ada baris judul di atas |
| Encoding | otomatis (UTF-8/CP1252), bisa `--encoding` | tidak relevan |
| Delimiter | otomatis (`,` `;` tab `|`), bisa `--delimiter` | tidak relevan |
| Tipe sel | semua teks | angka, tanggal, dan boolean asli dikonversi otomatis |

Konversi nilai dari Excel:

| Sel Excel | Nilai untuk mapping |
|---|---|
| teks | apa adanya |
| angka `87.5` | `87.5` (tanpa `42.0`, tanpa artefak float) |
| tanggal `12/05/1990` | `1990-05-12` |
| boolean TRUE/FALSE | `true` / `false` |
| sel kosong | string kosong |

Contoh impor Excel:

```bash
# sheet "Data", header di baris 2 (baris 1 judul laporan)
csmt import --csv nasabah.xlsx --sheet Data --header-row 2 \
    --mapping mapping.json --mode upsert

# atau pilih sheet berdasarkan urutan
csmt import --csv nasabah.xlsx --sheet 2 --mapping mapping.json --dry-run
```

> File `.xls` lama tidak didukung. Simpan dulu sebagai `.xlsx`.

#### Detail format CSV

| Aspek | Perilaku |
|---|---|
| Encoding | Otomatis: UTF-8 (termasuk BOM), CP1252, atau Latin-1. Bisa dipaksa dengan `--encoding` |
| Delimiter | Otomatis: koma `,`, titik koma `;`, tab, atau `\|`. Bisa dipaksa dengan `--delimiter` |
| Header | Baris pertama dianggap nama kolom. Spasi di awal/akhir dibuang, kolom kosong diberi nama `column_N` |
| Header duplikat | Diberi akhiran `_2`, `_3`, dan seterusnya, sehingga tidak ada kolom yang tertimpa |
| Baris kosong | Dilewati |
| Baris dengan jumlah kolom kurang | Kolom yang hilang dianggap kosong |
| Tanpa header | Gunakan `--no-header`. Kolom akan bernama `column_1`, `column_2`, dan seterusnya |

Contoh file (`examples/contacts.csv`):

```csv
Nama Lengkap,Email,Tanggal Lahir,Negara,Skor
Andi Pratama,andi.pratama@example.com,12/05/1990,Indonesia,87.5
Siti Rahmawati,siti.rahmawati@example.com,03/11/1985,Indonesia,91
John Smith,john.smith@example.com,2024-02-29,Singapore,78.25
```

### 5.2 Melihat field entity

```bash
uv run csmt schema --entity Contact
```

```
Entity set: Contact (8 fields)
  Active            Edm.Boolean
  BirthDate         Edm.DateTime
  CountryId         Edm.Guid
  Email             Edm.String
  Id                Edm.Guid
  Name              Edm.String
  ProcessListeners  Edm.Int32
  UsrScore          Edm.Decimal
```

Gunakan `--json` untuk keluaran dalam format JSON. Nama field di mapping harus sama
persis dengan nama di daftar ini (huruf besar-kecil juga berpengaruh).

### 5.3 Membuat kerangka mapping

```bash
uv run csmt init-mapping \
    --csv examples/contacts.csv \
    --entity Contact \
    --key Email \
    --out mapping.json
```

Perintah ini membaca header CSV dan membuat file JSON yang memetakan setiap kolom ke
field dengan nama yang sama. Tipe ditebak dari nama kolom (`Tanggal` menjadi `date`,
`Skor` menjadi `decimal`, `Id` menjadi `guid`). Tebakan ini hanya kerangka. Periksa dan
sesuaikan sebelum impor.

### 5.4 Mengedit mapping

Contoh mapping lengkap (`examples/mapping.json`):

```json
{
  "entity": "Contact",
  "key_fields": ["Email"],
  "constants": {"ProcessListeners": 0},
  "defaults": {"Active": true},
  "fields": {
    "Nama Lengkap": {"target": "Name", "required": true, "transform": "collapse_spaces"},
    "Email": {"target": "Email", "required": true, "transform": "lower"},
    "Tanggal Lahir": {"target": "BirthDate", "type": "date"},
    "Negara": {
      "target": "CountryId",
      "type": "lookup",
      "lookup": {"entity": "Country", "key_field": "Name"}
    },
    "Skor": {"target": "UsrScore", "type": "decimal"}
  }
}
```

**Bagian mapping:**

| Bagian | Fungsi |
|---|---|
| `entity` | Entity set tujuan |
| `key_fields` | Field untuk mencocokkan record yang sudah ada (mode `update`/`upsert`) |
| `constants` | Nilai tetap untuk setiap baris |
| `defaults` | Nilai yang dipakai bila field tidak diisi di CSV |
| `fields` | Pemetaan kolom CSV ke field Creatio |

**Pengaturan setiap kolom (`fields`):**

| Pengaturan | Nilai | Keterangan |
|---|---|---|
| `target` | nama field | Field tujuan di Creatio. Bila kosong, memakai nama kolom |
| `type` | `string`, `guid`, `int`, `decimal`, `bool`, `date`, `datetime`, `lookup` | Tipe konversi. Default `string` |
| `transform` | `trim`, `upper`, `lower`, `title`, `capitalize`, `collapse_spaces`, `digits`, `digits_plus_prefix`, `strip_zero_width` | Dijalankan sebelum konversi tipe |
| `required` | `true` / `false` | Baris gagal bila kolom ini kosong |
| `default` | nilai | Dipakai bila kolom kosong. Untuk `lookup`, isinya nama tampilan, yang juga di-resolve ke ID |
| `null_for_empty` | `true` / `false` | Kirim `null` bila kosong (berguna untuk mengosongkan field saat update) |
| `format` | format `strptime` | Format tanggal, mis. `%d/%m/%Y` |
| `choices` | objek | Pemetaan nilai CSV ke nilai Creatio, mis. `{"L": "Laki-laki", "P": "Perempuan"}` |
| `max_length` | angka | Batas panjang teks |
| `lookup` | `{entity, key_field}` | Mencari ID record lain berdasarkan nama. Pencocokan tidak case-sensitive |

**Aturan konversi yang perlu diketahui:**

- **Tanggal ambigu.** Tanpa `format`, tanggal dicoba dalam urutan DD/MM/YYYY lebih dulu.
  Jadi `03/11/1985` dibaca sebagai 3 November 1985. Jika file berasal dari sistem dengan
  format MM/DD/YYYY, tentukan `"format": "%m/%d/%Y"` secara eksplisit.
- **Angka.** `1.234,56` dan `87,5` dibaca sebagai format Indonesia. `1,234` dibaca
  sebagai seribu dua ratus tiga puluh empat, karena koma diikuti tiga digit dianggap
  pemisah ribuan. Kalau ragu, bersihkan dulu datanya.
- **Nilai kosong.** Kolom kosong dilewati, kecuali diatur `required`, `default`, atau
  `null_for_empty`.

### 5.5 Memeriksa mapping terhadap Creatio

Tambahkan `--validate-fields` agar nama field di mapping dicek ke `$metadata` sebelum
impor dimulai:

```bash
uv run csmt import --csv data.csv --mapping mapping.json --validate-fields --dry-run
```

Jika ada field yang tidak dikenal, impor berhenti sebelum mengirim satu baris pun.

---

## 6. Menjalankan impor

### 6.1 Langkah yang disarankan

```bash
# 1. Simulasi. Tidak menulis ke Creatio dan tidak membuat journal
uv run csmt import --csv data.csv --mapping mapping.json --dry-run

# 2. Impor sebenarnya, dengan batch 50 baris per request
uv run csmt import --csv data.csv --mapping mapping.json \
    --mode upsert --batch-size 50

# 3. Jika terputus (listrik, jaringan, dan sebagainya), lanjutkan dengan --resume
uv run csmt import --csv data.csv --mapping mapping.json \
    --mode upsert --batch-size 50 --resume
```

Saat dry-run, setiap baris berstatus `planned`. Periksa jumlahnya, lalu jalankan impor
sebenarnya.

Contoh keluaran dry-run:

```
Import summary
--------------
              total: 3
            created: 0
            updated: 0
            skipped: 0
            planned: 3
             failed: 0
            unknown: 0
            resumed: 0
```

### 6.2 Mode impor

| Mode | Perilaku | Kapan dipakai |
|---|---|---|
| `upsert` (default) | Record dengan key yang sama di-update. Yang belum ada dibuat | Paling aman untuk sebagian besar impor |
| `create` | Selalu membuat record baru | Data baru yang dipastikan belum ada di Creatio. Tambahkan `--skip-existing` bila record dengan key yang sama harus dilewati |
| `update` | Hanya meng-update record yang sudah ada. Yang tidak ditemukan dianggap gagal | Memperbarui data yang sudah pasti ada. Tambahkan `--create-missing` bila yang tidak ditemukan boleh dibuat |

`update` dan `upsert` membutuhkan key. Key diambil dari `key_fields` di mapping, atau dari
`--key`:

```bash
uv run csmt import --csv data.csv --mapping mapping.json --key Email
```

### 6.3 Opsi penting

| Flag | Fungsi |
|---|---|
| `--dry-run` | Simulasi. Tidak menulis ke Creatio dan tidak mencatat journal |
| `--limit N` | Hanya memproses N baris pertama. Berguna untuk uji coba |
| `--batch-size N` | Jumlah operasi per request `$batch`, 1 sampai 100. Default 50 |
| `--no-batch` | Satu request per baris. Lebih lambat, tetapi lebih mudah ditelusuri |
| `--resume` | Melewati baris yang sudah sukses di journal sebelumnya |
| `--duplicate-policy` | Cara menangani key yang sama di dalam CSV: `error` (default), `first_wins`, atau `last_wins` |
| `--fail-fast` | Berhenti pada kegagalan pertama |
| `--max-errors N` | Berhenti setelah N kegagalan |
| `--validate-fields` | Memeriksa field mapping ke `$metadata` sebelum impor |
| `--strict-sources` | Gagal bila CSV tidak punya kolom yang dipakai mapping. Default-nya hanya peringatan |
| `--report FILE` / `--no-report` | Lokasi report CSV, atau nonaktifkan |
| `--journal FILE` / `--no-journal` | Lokasi journal, atau nonaktifkan |
| `--encoding`, `--delimiter` | Memaksa encoding atau delimiter CSV |
| `--no-header` | CSV tanpa baris header |
| `--log-level`, `--log-file`, `--quiet` | Pengaturan log |

### 6.4 Resume

Setiap baris yang selesai dicatat di journal (`<csv>.journal.jsonl` secara default).
Dengan `--resume`, baris yang sudah sukses dilewati.

Journal memeriksa dua hal. Nomor baris harus sama, dan key baris tersebut juga harus
sama. Jika CSV diedit sehingga baris bergeser, baris yang isinya berubah akan diproses
ulang, bukan dilewati diam-diam.

> **Catatan.** Jika CSV diubah di antara dua run, `upsert` adalah pilihan paling aman.
> Mode `create` tanpa `--skip-existing` bisa membuat duplikat untuk baris yang bergeser.

### 6.5 Key duplikat di dalam CSV

Jika key yang sama muncul lebih dari sekali di file:

| `--duplicate-policy` | Perilaku |
|---|---|
| `error` (default) | Baris kedua ditandai gagal. Baris pertama tetap diproses |
| `first_wins` | Baris pertama diproses. Baris berikutnya dilewati |
| `last_wins` | Baris terakhir yang dipakai. Berlaku untuk `upsert` dan `update` |

---

## 6.6 Ekspor data dari Creatio ke CSV

Kebalikan dari impor: data ditarik dari Creatio lalu ditulis ke CSV. Berguna untuk
backup, audit, atau menyiapkan file untuk diubah lalu diimpor kembali.

```bash
# semua field
csmt export --entity Contact --out contacts.csv

# pilih kolom dan batasi baris
csmt export --entity Contact --out contacts.csv \
    --select Name,Email,CountryId,UsrScore --limit 1000

# filter dan urutan (ekspresi OData diteruskan apa adanya)
csmt export --entity Contact --out terbaru.csv \
    --filter "CreatedOn ge 2026-01-01" --orderby "CreatedOn desc"

# ganti GUID lookup menjadi caption
csmt export --entity Contact --out contacts.csv \
    --select Name,CountryId --expand-lookups CountryId=Country.Name

# output Excel: format mengikuti ekstensi .xlsx
csmt export --entity Contact --out contacts.xlsx --select Name,Email \
    --expand-lookups CountryId=Country.Name --sheet-name Nasabah
```

| Flag | Fungsi |
|---|---|
| `--out` | File tujuan. Default `<entity>-export-<waktu>.csv` |
| `--select` | Kolom yang diambil, dipisah koma. Default: semua field |
| `--filter` | Ekspresi `$filter`, mis. `"UsrScore ge 80"` |
| `--orderby` | Urutan, mis. `"Name asc"` atau `"CreatedOn desc"` |
| `--limit N` | Berhenti setelah N baris |
| `--page-size N` | Record per request. Default 500 |
| `--expand-lookups FIELD=ENTITY[.CAPTION]` | Ganti GUID dengan caption. Bisa diulang |
| `--delimiter`, `--encoding` | CSV saja. Default `,` dan `utf-8-sig` (BOM, agar Excel benar membaca UTF-8) |
| `--format auto\|csv\|xlsx` | Default `auto`: mengikuti ekstensi `--out` |
| `--sheet-name` | Nama worksheet untuk output `.xlsx` (default: nama entity) |
| `--no-header` | Tanpa baris header |

Cara kerja:

- Dibaca per halaman (`$top`/`$skip`) dan **berhenti otomatis** saat halaman terakhir
  mengembalikan baris lebih sedikit dari `--page-size`. Memori tetap kecil untuk entity
  besar karena tidak pernah menampung seluruh hasil.
- Karena paging memakai `$skip`, urutan data di server sebaiknya stabil selama export.
  Untuk data yang berubah saat export berjalan, tambahkan `--orderby` (mis.
  `--orderby "CreatedOn asc"`) agar tidak ada baris yang terlewat atau terbaca dua kali.
- File ditulis sambil berjalan, bukan menunggu semua data terkumpul.
- GUID lookup di-resolve berkelompok (bukan satu request per baris), lalu di-cache.
  Bila lookup gagal karena error server, GUID asli dipertahankan agar data tidak hilang.
- Tanpa `--select`, kolom mengikuti field pada record pertama.
- Output `.xlsx` ditulis dengan mode streaming (`write_only`), header dibekukan, dan
  lebar kolom disesuaikan. Tanggal dan desimal ditulis sebagai teks ISO/desimal agar
  hasil ekspor bisa diimpor kembali tanpa perbedaan format antar-locale Excel; angka
  dan boolean tetap bertipe asli.

Alur bolak-balik yang aman:

```bash
csmt export --entity Contact --out contacts.csv --select Name,Email
# edit contacts.csv, lalu impor kembali:
csmt import --csv contacts.csv --mapping mapping.json --mode upsert --key Email
```

Gunakan `--mode upsert` saat mengimpor hasil ekspor, supaya record yang sudah ada
diperbarui, bukan diduplikasi.

---

## 7. Membaca hasil impor

Setiap impor menghasilkan tiga artefak:

| File | Lokasi default | Isi |
|---|---|---|
| Report CSV | `<csv>-report-<waktu>.csv` | Satu baris per data CSV dengan statusnya |
| Summary JSON | `<report>.summary.json` | Ringkasan angka dalam format JSON |
| Journal | `<csv>.journal.jsonl` | Riwayat per baris, dipakai oleh `--resume` |

### 7.1 Ringkasan di terminal

```
Import summary
--------------
              total: 3
            created: 1
            updated: 0
            skipped: 0
            planned: 0
             failed: 3
            unknown: 0
            resumed: 0
   duration_seconds: 0.01
        report_path: r.csv
       journal_path: j.jsonl
```

| Angka | Arti |
|---|---|
| `total` | Jumlah baris yang diproses |
| `created` | Record baru dibuat |
| `updated` | Record yang sudah ada diperbarui |
| `skipped` | Dilewati (sudah ada, duplikat, atau sudah selesai di run sebelumnya) |
| `planned` | Hanya pada dry-run: baris yang akan diproses |
| `failed` | Gagal. Lihat kolom `error` di report |
| `unknown` | Hasilnya tidak bisa dipastikan. [Lihat bagian 10](#10-pemecahan-masalah) |
| `resumed` | Dilewati karena sudah selesai di run sebelumnya |

### 7.2 Isi report CSV

Contoh dari impor yang sebagian gagal:

```csv
row,key,action,status,http_status,record_id,error
3,row:3,map,failed,,,Nama Lengkap: value is required but empty
4,row:4,map,failed,,,Tanggal Lahir: '32/13/1990' is not a recognised date
5,Email=dewi@example.com,map,failed,,,CountryId: lookup value 'Atlantis' was not found in Country.Name
2,Email=budi@example.com,create,created,201,b8fd2f13-1a5a-4bb1-bf5c-8934810c8596,
```

| Kolom | Arti |
|---|---|
| `row` | Nomor baris di file CSV (baris 1 adalah header) |
| `key` | Key yang dipakai untuk pencocokan. Baris yang gagal sebelum key terbentuk diberi `row:N` |
| `action` | `create`, `update`, `map` (gagal di tahap konversi), `skip`, atau `resume` |
| `status` | `created`, `updated`, `skipped`, `planned`, `failed`, atau `unknown` |
| `http_status` | Kode HTTP dari Creatio, bila ada |
| `record_id` | ID record di Creatio, bila ada |
| `error` | Penjelasan kegagalan |

Urutan baris di report tidak selalu sama dengan urutan di CSV. Gunakan kolom `row` untuk
mencocokkan.

### 7.3 Kode keluar (exit code)

| Kode | Arti |
|---|---|
| `0` | Berhasil. Tidak ada baris gagal atau tidak pasti |
| `1` | Impor selesai, tetapi ada baris `failed` atau `unknown` |
| `2` | Konfigurasi, kredensial, atau koneksi bermasalah. Impor tidak dijalankan |
| `130` | Dihentikan dengan Ctrl+C |

Kode ini bisa dipakai di skrip, misalnya:

```bash
uv run csmt import --csv data.csv --mapping mapping.json --quiet
if [ $? -eq 1 ]; then echo "Ada baris gagal, cek report"; fi
```

---

## 8. Perintah lain

| Perintah | Fungsi | Contoh |
|---|---|---|
| `wizard` | Membuat file `.env` secara interaktif | `uv run csmt wizard` |
| `check` | Menguji URL, kredensial, dan akses entity. Tidak menulis apa pun | `uv run csmt check --entity Contact` |
| `schema` | Menampilkan field dan tipe entity dari `$metadata` | `uv run csmt schema --entity Contact --json` |
| `init-mapping` | Membuat kerangka mapping dari header CSV | `uv run csmt init-mapping --csv data.csv --out mapping.json` |
| `export` | Menarik data dari Creatio ke CSV | `uv run csmt export --entity Contact --out contacts.csv` |
| `import` | Mengimpor CSV ke Creatio | Lihat [bagian 6](#6-menjalankan-impor) |

Lihat bantuan lengkap dari perintah apa pun dengan `--help`:

```bash
uv run csmt import --help
```

---

## 9. Contoh alur lengkap

Contoh ini memakai site cloud dengan OAuth 2.0.

```bash
# 1. Buat konfigurasi (sekali saja)
uv run csmt wizard

# 2. Pastikan koneksi berhasil
uv run csmt check --entity Contact

# 3. Lihat field yang tersedia
uv run csmt schema --entity Contact

# 4. Buat kerangka mapping, lalu edit mapping.json
uv run csmt init-mapping --csv nasabah-2026-01.csv --entity Contact --key Email --out mapping-nasabah.json

# 5. Simulasi
uv run csmt import --csv nasabah-2026-01.csv --mapping mapping-nasabah.json --dry-run --validate-fields

# 6. Uji coba dengan 20 baris
uv run csmt import --csv nasabah-2026-01.csv --mapping mapping-nasabah.json --mode upsert --limit 20

# 7. Impor sebenarnya
uv run csmt import --csv nasabah-2026-01.csv --mapping mapping-nasabah.json --mode upsert --batch-size 50

# 8. Jika ada baris gagal: perbaiki CSV, lalu jalankan ulang dengan --resume
uv run csmt import --csv nasabah-2026-01.csv --mapping mapping-nasabah.json --mode upsert --resume
```

Untuk mapping baru, jalankan langkah 6 dulu di lingkungan uji, atau pada data produksi
dengan `--limit` kecil. Periksa hasilnya di Creatio sebelum menjalankan langkah 7.

---

## 10. Pemecahan masalah

| Gejala | Kemungkinan penyebab | Tindakan |
|---|---|---|
| `Cannot pick an authentication method` | Tidak ada kredensial di konfigurasi | Jalankan `wizard`, atau isi `CREATIO_AUTH_MODE` dan kredensialnya |
| `auth mode 'oauth2' requires --identity-url` | Identity service URL belum ada, dan domain bukan `*.creatio.com` | Isi `CREATIO_IDENTITY_URL` dengan nilai `OAuth20IdentityServerUrl` |
| `Login rejected (Code=1)` | Username atau password salah, atau akun terkunci | Periksa kredensial. Coba login manual ke Creatio dengan akun yang sama |
| `Login failed with HTTP 404` | URL site salah | Periksa `CREATIO_BASE_URL` |
| `HTTP 404` pada request OData, `Not found: /odata/...` | `CREATIO_ODATA_PATH` tidak sesuai runtime | Gunakan `/odata` untuk .NET 8, atau `/0/odata` untuk .NET Framework |
| `HTTP 403` pada request OData (Forms) | Cookie `BPMCSRF` tidak valid, atau user tidak punya hak akses | Periksa hak akses user integrasi. Jalankan ulang impor |
| `HTTP 401` berulang | Token OAuth ditolak, atau kredensial OAuth salah | Periksa client ID, client secret, dan identity service URL |
| `OAuth token request failed with HTTP 400` | Client ID, secret, atau `grant_type` salah, atau identity service URL salah | Periksa nilai system setting `OAuth20IdentityServerUrl` |
| `TLS` / `SSL` error | Sertifikat tidak bisa diverifikasi | Perbaiki sertifikat di server. `--insecure` hanya untuk development |
| `Timeout` | Jaringan lambat atau server sibuk | Naikkan `CREATIO_TIMEOUT`. Gunakan `--resume` untuk melanjutkan |
| `is not a recognised date` | Format tanggal tidak dikenali | Tambahkan `"format"` di mapping, mis. `%d/%m/%Y` |
| `lookup value '...' was not found` | Nama di CSV tidak sama dengan data di Creatio | Periksa ejaan, atau buat datanya di Creatio dulu |
| `must be unique` | Nilai unik sudah dipakai record lain | Periksa key atau nilai duplikat |
| `key field ... is empty` | Kolom key kosong di CSV | Isi atau hapus baris tersebut |
| `duplicate key in CSV` | Key yang sama muncul lebih dari sekali | Perbaiki CSV, atau atur `--duplicate-policy` |
| `CSV is missing column(s)` | Kolom yang dipakai mapping tidak ada di CSV | Periksa header. Gunakan `--strict-sources` agar ini menjadi error |
| Status `unknown` | Response Creatio hilang atau tidak bisa dibaca, sehingga hasil tidak bisa dipastikan | Jalankan ulang dengan `--mode upsert`. Upsert aman untuk diulang karena dicocokkan dengan key. Pastikan hasilnya di Creatio |
| Status `unknown` tanpa key | Tidak ada key untuk memeriksa ulang record | Periksa manual. Tambahkan `key_fields` pada impor berikutnya |
| Ekspor menghasilkan kolom kosong | Nama di `--select` salah (harus kode field, case-sensitive) | Cek dengan `csmt schema --entity <Entity>` |
| Kolom lookup berisi GUID | `--expand-lookups` belum dipakai | Tambahkan `--expand-lookups CountryId=Country.Name` |
| Lookup gagal di-resolve | Nama entity/caption salah, atau error server | Periksa `ENTITY.CAPTION`, mis. `Country.Name` |
| `SSL: CERTIFICATE_VERIFY_FAILED ... Hostname mismatch` pada `/connect/token` | Hostname identity service salah (mis. `_is` padahal seharusnya `-is`) | Set `--identity-url https://<subdomain>-is.creatio.com`, atau salin nilai `OAuth20IdentityServerUrl` dari System settings |
| `404` pada `/connect/token` | Host bukan identity server | Sama seperti di atas; verifikasi dengan `curl` seperti pada bagian 4.5 |
| `HTTP 403` + `Current user does not have sufficient permissions to use OData` | User integrasi belum punya izin OData | Creatio → **Setup → Users → System operation permissions**, aktifkan operation **"Access to OData"** (code `CanUseODataService`) untuk user tersebut |
| `Insufficient permissions to add record in object` | Izin object kurang | Beri hak baca/tambah/ubah pada object tersebut (System Designer → Access rights to objects) |
| Respons HTML halaman login pada `/odata` | Path OData salah untuk instance ini | Gunakan `/0/odata`; wizard sudah mencoba keduanya otomatis |
| CSV berantakan di Excel | Encoding/delimiter | Default `utf-8-sig` + `,`. Untuk Excel Indonesia kadang perlu `--delimiter ";"` |
| `.xls` ditolak | Format lama tidak didukung | Simpan sebagai `.xlsx` |
| Sheet tidak ditemukan | Nama sheet salah | Pesan error menyebut daftar sheet yang tersedia |
| Kolom kosong dari Excel | Header tidak sejajar | Pastikan `--header-row` menunjuk baris header yang benar |

Bila masalah tidak tercantum di atas, jalankan dengan log detail:

```bash
uv run csmt import --csv data.csv --mapping mapping.json --log-level DEBUG --log-file debug.log
```

---

## 11. Praktik aman

- **User integrasi khusus.** Jangan pakai Supervisor. Beri hak akses hanya ke objek yang
  dibutuhkan.
- **File `.env` tidak boleh masuk git.** Sudah dicakup oleh `.gitignore`. Wizard membuat
  file dengan permission `600`.
- **Selalu dry-run dulu** untuk file baru atau mapping yang baru diubah.
- **Uji dengan `--limit`** sebelum impor penuh.
- **Simpan report dan journal** setiap impor. Keduanya adalah bukti apa yang terjadi.
- **Ganti client secret** jika pernah terbagi ke pihak lain.
- **Jangan pakai `--insecure`** di produksi.

---

## 12. Lampiran: referensi cepat

```bash
# Setup (sekali saja)
uv run csmt wizard

# Cek
uv run csmt check --entity Contact
uv run csmt schema --entity Contact

# Mapping
uv run csmt init-mapping --csv data.csv --entity Contact --key Email --out mapping.json

# Ekspor
uv run csmt export --entity Contact --out contacts.csv --select Name,Email --expand-lookups CountryId=Country.Name

# Impor
uv run csmt import --csv data.csv --mapping mapping.json --dry-run
uv run csmt import --csv data.csv --mapping mapping.json --mode upsert --batch-size 50
uv run csmt import --csv data.csv --mapping mapping.json --mode upsert --resume
```
