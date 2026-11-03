# Autentikasi ke Creatio — referensi URL & konfigurasi

Dokumen ini merangkum URL autentikasi yang dipakai `csmt`, berdasarkan
referensi resmi Creatio Academy (Authentication basics, versi 10):

- Overview: <https://academy.creatio.com/guides/dev/development-on-creatio-platform/integrations-and-api/authentication/authentication-basics/overview>
- Examples (C# console app): <https://academy.creatio.com/guides/dev/development-on-creatio-platform/integrations-and-api/authentication/authentication-basics/examples/implement-authentication-c-sharp>
- References — AuthService.svc: <https://academy.creatio.com/guides/dev/development-on-creatio-platform/integrations-and-api/authentication/authentication-basics/references/authservice-svc>
- OAuth 2.0 — client credentials grant: <https://academy.creatio.com/guides/dev/development-on-creatio-platform/integrations-and-api/authentication/oauth-2-0-authorization/identity-service-overview>
- OAuth 2.0 — access token lifetime: <https://academy.creatio.com/guides/dev/development-on-creatio-platform/integrations-and-api/authentication/oauth-2-0-authorization/oauth-token-lifetime>

> Referensi lama `docs/7-18/...` (Creatio 7.18) sudah tidak dipakai.

## Rekomendasi resmi

Menurut Overview (v10): *"We recommend using OAuth 2.0 authentication to integrate
external services with Creatio."* Forms authentication tetap didukung, tetapi cookie
dari `AuthService.svc` harus dikirim ulang di setiap request. Basic authentication
hanya disarankan bila perlu, dan membutuhkan perlindungan CSRF dimatikan di server.

| Metode | `--auth-mode` | Kapan dipakai |
|---|---|---|
| OAuth 2.0 client credentials | `oauth2` | **Direkomendasikan**. Integrasi server-ke-server |
| Forms (cookie) | `forms` | Didukung; kredensial user dipakai untuk login |
| Basic | `basic` | Hanya bila `UseCsrfToken=false` di server |

## 1. OAuth 2.0 client credentials (`oauth2`)

**Endpoint token:**

```
POST {IdentityServiceURL}/connect/token
Content-Type: application/x-www-form-urlencoded

client_id=...&client_secret=...&grant_type=client_credentials
```

- `IdentityServiceURL` adalah nilai system setting **`OAuth20IdentityServerUrl`**
  ("Authorization server Url for OAuth 2.0 integrations"). Ini **tidak sama** dengan
  URL site Creatio.
  - **Default site cloud:** `https://<subdomain>-is.creatio.com` (**hyphen**).
    Contoh: `https://pegadaian.creatio.com` → `https://pegadaian-is.creatio.com`.
    Sebagian tenant memakai bentuk underscore (`<subdomain>_is`); validator mencoba
    keduanya, hyphen lebih dulu.
  - **Domain lain (on-site / custom):** tidak ada default, isi lewat `--identity-url` /
    `CREATIO_IDENTITY_URL`.
  - Wizard selalu menampilkan nilai default ini dan tetap menanyakannya. Setelah diisi,
    wizard langsung memakai client ID/secret yang baru dimasukkan untuk memanggil
    `{identity}/connect/token`, sehingga satu langkah memvalidasi host **dan** kredensial:

    | Hasil | Arti |
    |---|---|
    | `200` + `access_token` | host dan kredensial valid |
    | `400/401` JSON (`invalid_client`, `invalid_grant`) | host benar (itu identity server), kredensial ditolak |
    | TLS mismatch / `404` | hostname salah |

    Kredensial asli dipakai (bukan dummy) agar tidak ada percobaan login gagal yang tidak
    perlu dan agar tidak ada ambiguitas "host salah atau secret salah".
  - **Jangan menebak.** Cara paling pasti: buka Creatio → **Setup → System settings** →
    cari kode `OAuth20IdentityServerUrl`, lalu salin nilainya ke `--identity-url`.

  Cara cepat memeriksa sebuah kandidat host:

  ```bash
  curl -sS -X POST https://<kandidat>/connect/token \
      -d "grant_type=client_credentials&client_id=probe&client_secret=probe"
  # {"error":"invalid_client"}  -> host benar (kredensial yang salah)
  # 404 / TLS hostname mismatch -> host salah
  ```
- Respons `200 OK`: `access_token`, `expires_in` (default **3600** detik),
  `token_type: Bearer`, `scope`.

**Request ke OData** memakai header:

```
Authorization: Bearer <access_token>
```

Dokumen tidak menyebut header `BPMCSRF` untuk jalur Bearer, sehingga importer tidak
mengirimnya untuk mode ini.

**Masa berlaku token:** kedaluwarsa setelah `expires_in`. Respons `401 Unauthorized`
berarti token perlu diperbarui. Importer otomatis meminta token baru **sekali** lalu
mengulang request. Dokumen Creatio juga menyarankan lifetime tidak lebih dari beberapa
jam. Pada instance on-site, lifetime bisa diubah lewat kolom `AccessTokenLifetime` di
tabel `Clients`. Di cloud, hubungi support Creatio.

## 2. Forms authentication (`forms`)

```
POST {CreatioURL}/ServiceModel/AuthService.svc/Login
Content-Type: application/json; charset=utf-8
ForceUseSession: true

{"UserName": "...", "UserPassword": "..."}
```

- Respons `200 OK` dengan body `{"Code": 0, "Message": "", "Exception": null, ...}`.
  `Code = 0` berarti berhasil. `Code = 1` berarti gagal, dan `Message` menjelaskan
  penyebabnya.
- Respons menyertakan cookie autentikasi (`.ASPXAUTH`, `BPMCSRF`). Cookie inilah yang
  dikirim ulang pada setiap request.
- Request OData berikutnya memakai `BPMCSRF` sebagai header
  (`ForceUseSession: true` + `BPMCSRF: <nilai cookie>`). Jika tidak ada, server
  menjawab `403`.
- Akun yang dipakai harus punya hak akses yang cukup. Lihat bagian keamanan di bawah.

## 3. Basic authentication (`basic`)

HTTP Basic di setiap request. Creatio menyarankan mematikan perlindungan CSRF hanya
untuk mode ini, dengan salah satu cara berikut:

- semua service: `<add value="false" key="UseCsrfToken" />` di `Web.Config` (root
  dan `Terrasoft.WebApp\Web.Config`);
- path tertentu: `DisableCsrfTokenValidationForPaths`, mis.
  `/MsgUtilService.svc/Ping,/AuthService.svc/Login`.

## Keamanan — wajib dibaca

Dari Overview resmi:

- Buat **user khusus untuk integrasi** dan beri hanya hak yang dibutuhkan (objek,
  record, dan kolom).
- Jangan memakai **Supervisor**, dan jangan beri izin operasi **Can manage
  configuration elements** (`CanManageSolution`) kepada user integrasi.
- Untuk OAuth 2.0, buat client credentials khusus untuk aplikasi ini, jangan
  memakai ulang client milik sistem lain.

Contoh di `.env.example` memakai nama `Supervisor` hanya sebagai placeholder.
Ganti dengan user integrasi sebelum dipakai.

## Konfigurasi di `csmt`

| Env | Flag | Keterangan |
|---|---|---|
| `CREATIO_BASE_URL` | `--base-url` | URL site, mis. `https://mycreatio.example.com` |
| `CREATIO_ODATA_PATH` | `--odata-path` | `/odata` (.NET 8, default wizard) atau `/0/odata` (.NET Framework) |
| `CREATIO_AUTH_MODE` | `--auth-mode` | `oauth2`, `forms`, `basic`, atau `auto` |
| `CREATIO_CLIENT_ID` / `CREATIO_CLIENT_SECRET` | `--client-id` / `--client-secret` | untuk `oauth2` |
| `CREATIO_IDENTITY_URL` | `--identity-url` | untuk `oauth2`: nilai `OAuth20IdentityServerUrl` |
| `CREATIO_USERNAME` / `CREATIO_PASSWORD` | `--username` / `--password` | untuk `forms` dan `basic` |

Contoh `.env` untuk OAuth 2.0:

```env
CREATIO_BASE_URL=https://mycreatio.example.com
CREATIO_ODATA_PATH=/odata
CREATIO_AUTH_MODE=oauth2
CREATIO_CLIENT_ID=...
CREATIO_CLIENT_SECRET=...
CREATIO_IDENTITY_URL=https://mycreatio-is.example.com
CREATIO_ENTITY=Contact
```

Wizard (`uv run csmt wizard`) menanyakan nilai yang sama secara
berurutan.

## Cara memverifikasi

```bash
uv run csmt check --entity Contact
```

Perintah ini mengeksekusi autentikasi, memanggil service document, lalu membaca satu
record dari entity yang diminta.

## Catatan implementasi

- Urutan penentuan identity URL: `CREATIO_IDENTITY_URL` (atau flag / wizard), lalu
  default dari subdomain untuk `*.creatio.com`. Untuk domain lain tanpa nilai eksplisit,
  importer berhenti dengan pesan yang menunjuk ke system setting `OAuth20IdentityServerUrl`.
- Jika site memakai pola lain (mis. `-is`), isi nilainya secara eksplisit. Komunitas
  Creatio menyebut `-is` sebagai pola umum, tetapi itu bukan dokumentasi resmi.
