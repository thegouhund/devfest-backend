<div align="center">

# Family Health Monitor — Backend

**Monitoring kesehatan keluarga lewat kamera. Tanpa wearable, tanpa alat tambahan.**

Backend FastAPI untuk pengukuran vital sign berbasis rPPG, deteksi anomali statistik,
dan AI health companion dengan memory personal.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![TimescaleDB](https://img.shields.io/badge/TimescaleDB-2.29-FDB515?style=for-the-badge&logo=timescale&logoColor=black)](https://www.timescale.com/)
[![Tests](https://img.shields.io/badge/tests-514_passed-2EA043?style=for-the-badge&logo=pytest&logoColor=white)](#testing)

[Fitur](#fitur) · [Tech Stack](#tech-stack) · [Quick Start](#quick-start) · [Konfigurasi](#konfigurasi) · [Struktur](#struktur-proyek) · [Testing](#testing)

</div>

> [!IMPORTANT]
> **Bukan alat diagnostik medis.** Aplikasi ini bersifat informasional/wellness dan tidak
> menggantikan pemeriksaan dokter, ECG, atau pulse oximeter klinis. Akurasi rPPG dipengaruhi
> pencahayaan, gerakan, dan skin tone. Untuk keluhan kesehatan, konsultasikan ke tenaga medis.

---

## Apa Ini?

Keluarga tidak punya cara mudah memantau tren kesehatan dasar tanpa membeli wearable untuk
setiap anggota. Family Health Monitor menjawabnya dengan tiga hal:

- **Ukur pakai kamera biasa.** Teknik rPPG (*remote photoplethysmography*) membaca perubahan
  warna kulit wajah dari video untuk mengekstrak heart rate, HRV, dan respiration rate —
  cukup rekam wajah 30–60 detik.
- **Tahu kapan ada yang tidak biasa.** Sistem membangun baseline statistik personal per orang,
  lalu memberi peringatan lewat Telegram saat ada penyimpangan bermakna dari pola normalnya.
- **Tanya pakai bahasa sehari-hari.** Chatbot menjawab pertanyaan tentang data ("gimana detak
  jantung saya minggu ini?") dan mencatat aktivitas dari kalimat biasa seperti "baru ngopi
  2 cangkir".

Dirancang untuk satu keluarga (2–8 anggota), tapi skemanya sudah *multi-family-ready* sejak awal.

---

## Fitur

| Modul | Kemampuan |
|---|---|
| **Auth & Family** | Akun standalone atau family group, role admin/member, profil *dependent* untuk anak & lansia tanpa akun sendiri |
| **Privasi** | Kontrol visibilitas per jenis data — anggota bisa menandai data tertentu privat dari anggota lain |
| **Pengukuran** | Live capture via webcam atau upload video, pemrosesan rPPG server-side, indikator kualitas sinyal |
| **Statistik** | Tren harian/mingguan/bulanan, ringkasan min/max/rata-rata, overlay aktivitas pada grafik |
| **Anomali** | Baseline personal (rolling mean/stddev), deteksi z-score, konteks aktivitas terdekat |
| **Notifikasi** | Telegram bot, alert instan saat anomali terdeteksi |
| **Chatbot** | LangChain + DeepSeek, tool-calling ke data asli, mencatat aktivitas dari kalimat biasa |
| **Activity Log** | Dua entry point (quick-menu & chat) ke satu data model |

---

## Tech Stack

### Inti

| Teknologi | Versi | Peran |
|---|---|---|
| [Python](https://www.python.org/) | 3.12 | Runtime |
| [FastAPI](https://fastapi.tiangolo.com/) | 0.141 | Web framework, OpenAPI docs otomatis |
| [Uvicorn](https://www.uvicorn.org/) | 0.52 | ASGI server |
| [SQLAlchemy](https://www.sqlalchemy.org/) | 2.0 | ORM, gaya `Mapped[]` modern |
| [Alembic](https://alembic.sqlalchemy.org/) | 1.14 | Migrasi database |
| [Pydantic](https://docs.pydantic.dev/) | 2.13 | Validasi & serialisasi |

### Database

| Teknologi | Versi | Peran |
|---|---|---|
| [PostgreSQL](https://www.postgresql.org/) | 16 | Database utama |
| [TimescaleDB](https://www.timescale.com/) | 2.29 | Hypertable untuk time-series vitals |
| [pgvector](https://github.com/pgvector/pgvector) | 0.8 | Similarity search embedding (RAG) |

### Auth & Integrasi

| Teknologi | Versi | Peran |
|---|---|---|
| [PyJWT](https://pyjwt.readthedocs.io/) | 2.13 | Token akses |
| [bcrypt](https://github.com/pyca/bcrypt/) | 4.2 | Hashing password |
| [LangChain](https://www.langchain.com/) | 1.3 | Orkestrasi agent & tool-calling |
| [DeepSeek](https://www.deepseek.com/) | — | LLM chatbot, lewat antarmuka kompatibel-OpenAI |
| [Chainlit](https://chainlit.io/) | 2.12 | UI chat, proses terpisah yang di-embed sebagai iframe |
| [open-rppg](https://pypi.org/project/open-rppg/) | 0.1 | Ekstraksi vital sign dari video wajah |

### Kenapa TimescaleDB dan pgvector?

Keduanya ekstensi PostgreSQL, bukan database terpisah — jadi satu koneksi, satu backup,
satu transaksi. TimescaleDB mempartisi `vitals_readings` berdasarkan waktu supaya query
dashboard tetap cepat saat data historis menumpuk. pgvector menyimpan embedding riwayat
kesehatan untuk pencarian semantik chatbot. Image `timescaledb-ha` sudah memuat keduanya.

---

## Quick Start

### Prasyarat

- Python 3.12+
- Docker & Docker Compose (untuk PostgreSQL)

### 1. Clone & masuk folder

```bash
git clone https://github.com/thegouhund/devfest-backend.git
cd devfest-backend
```

### 2. Virtual environment & dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Konfigurasi environment

```bash
cp .env.example .env
```

Isi `JWT_SECRET` — tidak ada nilai default, dan aplikasi sengaja gagal keras kalau kosong:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 4. Jalankan database

```bash
docker compose up -d
```

PostgreSQL 16 + TimescaleDB + pgvector di port `5432`. Tunggu sampai siap:

```bash
docker compose exec db pg_isready -U postgres -d devfest
```

### 5. Migrasi database

```bash
alembic upgrade head
```

Ini membuat 16 tabel, mengaktifkan ekstensi, mengubah `vitals_readings` jadi hypertable,
dan mengisi seed `metric_types`.

### 6. Jalankan server

```bash
uvicorn app.main:app --reload
```

| Alamat | Isi |
|---|---|
| http://localhost:8000/health | Health check |
| http://localhost:8000/docs | Swagger UI |
| http://localhost:8000/redoc | ReDoc |

> [!NOTE]
> Startup memuat model rPPG lebih dulu (~20 detik) supaya pengukuran pertama
> tidak menanggung waktu kompilasi — terukur turun dari 24 detik jadi 1 detik.
> Saat pengembangan, `WARM_UP_RPPG_ON_START=false` membuat startup langsung siap.

### 7. Jalankan chatbot (opsional)

Chat berjalan sebagai **proses terpisah**, bukan bagian dari API:

```bash
chainlit run chat_app.py --port 8001
```

Frontend meng-embed-nya sebagai iframe dengan token yang sama:

```jsx
<iframe src={`http://localhost:8001?token=${accessToken}`} />
```

Di produksi, chat dilayani di bawah path `/chat` pada domain yang sama
dengan API — jadi cukup satu DNS record:

```jsx
<iframe src={`${API_URL}/chat?token=${accessToken}`} />
```

Butuh `DEEPSEEK_API_KEY`. Tanpa itu, chat menolak dengan pesan yang jelas
sementara API utama tetap berjalan normal — chatbot adalah tambahan, bukan syarat.

---

## API

31 endpoint aktif. Dokumentasi interaktif lengkap ada di `/docs` saat server berjalan;
kontrak untuk frontend ada di [`API_CONTRACT.md`](../devfest-md/API_CONTRACT.md).

| Domain | Endpoint | Fungsi |
|---|---|---|
| **Auth** | `POST /auth/register`, `POST /auth/login` | Daftar akun, login dapat token |
| **User** | `GET\|PATCH /users/me` | Profil sendiri, termasuk tinggi & berat |
| **Family** | `POST /families`, `POST /families/join` | Buat family, gabung pakai kode undangan |
| | `GET /families/{id}/members` | Daftar anggota |
| | `PATCH\|DELETE /families/{id}/members/{user_id}` | Ubah role, keluarkan anggota |
| | `POST /families/{id}/dependents` | Profil anak/lansia tanpa akun sendiri |
| | `GET /families/{id}/dashboard` | Ringkasan seluruh anggota |
| **Privasi** | `GET\|PUT /settings/visibility` | Atur data mana yang terlihat keluarga |
| **Pengukuran** | `POST /measurements/upload`, `POST /measurements/live` | Kirim video, diproses di latar belakang |
| | `GET /measurements/{id}` | Status pemrosesan, untuk polling |
| | `GET /measurements/{id}/results` | Hasil ukur beserta kualitas sinyal |
| | `GET /measurements` | Riwayat sesi |
| **Statistik** | `GET /vitals/trend` | Tren per hari, minggu, atau bulan |
| | `GET /vitals/summary` | Ringkasan plus perbandingan periode |
| **Aktivitas** | `GET\|POST /activities` | Catat dan lihat kegiatan harian |
| | `PATCH\|DELETE /activities/{id}` | Ubah atau hapus catatan |
| **Anomali** | `GET /anomalies`, `GET /anomalies/{id}` | Daftar dan detail beserta konteks penyebab |
| | `PATCH /anomalies/{id}` | Tandai sudah dibaca atau diabaikan |
| **Telegram** | `POST\|DELETE /telegram/link`, `GET /telegram/status` | Sambungkan dan putuskan notifikasi |

Semua endpoint di atas berprefiks `/api/v1`. Chat tidak lewat REST — lihat
[langkah 7](#7-jalankan-chatbot-opsional).

## Konfigurasi

Semua lewat environment variable. Lihat [`.env.example`](.env.example).

| Variable | Wajib | Default | Keterangan |
|---|:---:|---|---|
| `DATABASE_URL` | — | `postgresql+psycopg://postgres:postgres@localhost:5432/devfest` | Koneksi PostgreSQL |
| `JWT_SECRET` | **Ya** | — | Kunci penandatangan token. Minimal 32 byte; lebih pendek ditolak |
| `JWT_EXPIRE_MINUTES` | — | `1440` | Masa berlaku token (menit) |
| `VIDEO_STORAGE_PATH` | — | `./data/videos` | Lokasi video mentah di filesystem |
| `DEEPSEEK_API_KEY` | Chatbot | — | API key LLM. Tanpa ini chat nonaktif, API tetap jalan |
| `LLM_MODEL` | — | `deepseek-chat` | Model yang dipakai chatbot |
| `LLM_BASE_URL` | — | `https://api.deepseek.com` | Ganti untuk pindah penyedia LLM |
| `TELEGRAM_BOT_TOKEN` | Notifikasi | — | Token bot. Tanpa ini notifikasi tercatat `failed` |
| `TELEGRAM_BOT_USERNAME` | — | — | Username bot tanpa `@`, ditampilkan saat linking |
| `WARM_UP_RPPG_ON_START` | — | `true` | Muat model rPPG saat startup |
| `BACKEND_CORS_ORIGINS` | — | `http://localhost:5173,http://localhost:3000` | Origin frontend, dipisah koma |
| `ANOMALY_ZSCORE_THRESHOLD` | — | `2.0` | Ambang deviasi anomali |
| `BASELINE_COLD_START_DAYS` | — | `14` | Minimal hari data sebelum anomali aktif |
| `APP_NAME` | — | `Family Health Monitor` | Judul di OpenAPI docs |
| `ENVIRONMENT` | — | `local` | Penanda environment |

> [!NOTE]
> Secret (`JWT_SECRET`, `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`) sengaja tidak punya nilai
> default. Fallback yang "kelihatan jalan" lebih berbahaya daripada error di awal — token yang
> ditandatangani dengan secret kosong bisa dipalsukan siapa saja.

---

## Struktur Proyek

```
devfest-backend/
├── app/
│   ├── api/v1/           # Route handler, dipisah per domain
│   ├── core/
│   │   ├── config.py     # Settings dari environment
│   │   └── security.py   # Hashing, JWT, otorisasi
│   ├── db/
│   │   ├── models.py     # 16 model SQLAlchemy sesuai ERD
│   │   ├── seed.py       # Seed metric_types
│   │   └── session.py    # Engine & dependency get_db
│   ├── chat/
│   │   ├── agent.py      # Agent LangChain + prompt pengaman medis
│   │   ├── llm.py        # Pabrik model, penyedia bisa ditukar
│   │   ├── tools.py      # Tool di atas services/, id user diikat server
│   │   └── session.py    # Autentikasi & siklus sesi chat
│   ├── services/         # Logika bisnis, dipakai ulang REST & chatbot
│   ├── main.py           # Entry point FastAPI
│   └── schemas.py        # Skema request/response
├── alembic/versions/     # File migrasi
├── chat_app.py           # Aplikasi Chainlit, proses terpisah
├── tests/                # Test suite
├── docker-compose.yml    # PostgreSQL + TimescaleDB + pgvector
└── requirements.txt
```

Logika bisnis tinggal di `services/`, bukan di route handler — supaya REST endpoint dan
tool chatbot memanggil fungsi yang sama, dan aturan privasi berlaku identik di keduanya.

---

## Model Data

16 tabel. Yang perlu diketahui sebelum menyentuh skema:

**Metrik bukan enum.** `metric_types` adalah tabel lookup. Menambah metrik baru (SpO2,
stress score) cukup satu `INSERT` — tanpa migrasi, tanpa ubah kode.

**Subjek ≠ pelaku.** Beberapa tabel punya `user_id` (siapa yang jadi subjek data) terpisah
dari `*_by_user_id` (siapa yang menginput). Ini yang memungkinkan orang tua mencatat
pengukuran atau riwayat kesehatan atas nama anaknya.

**Vitals disimpan long-format.** Satu baris per metrik per waktu, bukan satu baris berisi
tiga kolom. Metrik baru tidak butuh kolom baru.

**Dependent tanpa login.** Ditangani lewat self-FK `managed_by_user_id` di tabel `users`,
bukan tabel terpisah. `email` dan `password_hash` boleh NULL.

> `vitals_readings` punya primary key gabungan `(id, recorded_at)` — TimescaleDB mewajibkan
> kolom partisi ikut dalam primary key. `id` tetap UUID unik, jadi perilakunya praktis sama.

Detail lengkap ada di [`ERD.md`](../devfest-md/ERD.md).

---

## Testing

```bash
pytest                              # 514 test
pytest -v                           # verbose
pytest tests/test_security.py       # satu file
```

Test yang butuh PostgreSQL (migrasi, hypertable, pgvector) di-skip otomatis kalau database
tidak tersedia. Untuk menjalankannya:

```bash
TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/devfest" pytest
```

Test yang menjalankan model rPPG sungguhan ditandai `slow` dan dilewati secara
default karena memuat JAX (~20 detik):

```bash
pytest -m slow
```

> [!WARNING]
> `TEST_DATABASE_URL` akan **menghapus seluruh isi schema** database yang ditunjuk.
> Jangan arahkan ke database yang berisi data asli.

---

## Keamanan

Data vital sign dan video wajah adalah data biometrik sensitif. Yang sudah diterapkan:

- **Password** di-hash bcrypt dengan garam acak per user
- **Login tidak membocorkan email terdaftar** — verifikasi hash tetap dijalankan walau email
  tidak ada, supaya waktu respons tidak bisa dipakai memetakan siapa yang punya akun
- **Token JWT** wajib punya `exp` dan `sub`, algoritma dibatasi eksplisit (menutup serangan `alg=none`)
- **User nonaktif langsung kehilangan akses**, tanpa menunggu token kedaluwarsa
- **Kunci JWT minimal 32 byte** sesuai RFC 7518 — kunci pendek ditolak, bukan sekadar
  diperingatkan, karena kunci lemah bisa dipecahkan untuk memalsukan token
- **Video mentah** disimpan di filesystem terpisah dari database, tidak pernah masuk git
- **Path video dibangun dari UUID tervalidasi saja**, dan isi berkas diperiksa lewat
  signature kontainer — ekstensi `.mp4` terlalu mudah dipalsukan
- **Direktori video ber-permission ketat** (`0700`/`0600`) supaya tidak terbaca user lain
  di VPS bersama
- **Satu otoritas privasi** (`accessible_user_ids`) dipakai semua endpoint data kesehatan,
  jadi tidak ada jalur yang bisa lupa memeriksanya
- **Chatbot tidak menerima id user dari model** — identitas diikat di server, supaya
  model tidak bisa dibujuk membaca data orang lain

Untuk deployment: aktifkan enkripsi disk (mis. LUKS) pada volume tempat video disimpan, dan
pastikan semua transmisi lewat HTTPS.

---

## Roadmap

| Fase | Lingkup | Status |
|---|---|:---:|
| **1** | Auth & family, capture + rPPG, statistik, activity log, notifikasi Telegram | ✅ Selesai |
| **2** | Chatbot DeepSeek + LangChain, activity via chat | ✅ Selesai |
| — | Memory RAG (`health_facts` + pgvector) | ⏸️ Ditunda, lihat catatan di bawah |
| **3** | Anomaly detection berbasis ML, family dashboard lanjutan, evaluasi PWA | ⏳ |

> [!NOTE]
> **Memory RAG ditunda.** ERD menetapkan `vector(1536)`, sementara DeepSeek
> tidak menyediakan model embedding — jadi butuh penyedia terpisah (OpenAI,
> atau model lokal dengan dimensi berbeda). Tanpa ini chatbot tetap bisa
> menjawab pertanyaan data lewat tools dan mencatat aktivitas, hanya belum
> mengingat cerita riwayat kesehatan antar sesi.

---

## Dokumentasi Terkait

| Dokumen | Isi |
|---|---|
| [`PRD.md`](../devfest-md/PRD.md) | Product requirements, functional requirements, user flow |
| [`ERD.md`](../devfest-md/ERD.md) | Skema database lengkap per tabel & kolom |

---

<div align="center">

Dibangun untuk DevFest · FastAPI · PostgreSQL · TimescaleDB · pgvector

</div>
