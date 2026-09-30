# Early Warning System Presensi Pegawai

Dashboard Streamlit untuk monitoring kepatuhan presensi, Early Warning System (EWS), verifikasi tindak lanjut, dan laporan TK berbasis PostgreSQL & Docker.

---

## 🚀 Menjalankan Aplikasi via Docker

Aplikasi ini telah dikonfigurasi penuh menggunakan Docker Compose sehingga pengembang tidak perlu melakukan setup environment Python atau PostgreSQL secara manual di mesin lokal.

### 1. Prasyarat
- [Docker](https://docs.docker.com/get-docker/) & Docker Compose terinstal di komputer Anda.

### 2. Memulai Layanan
Jalankan perintah berikut di direktori root proyek untuk membangun image dan menyalakan container (PostgreSQL & Streamlit):

```bash
docker compose up --build -d
```

Setelah seluruh container berjalan:
- **Dashboard Streamlit**: Buka browser di [http://localhost:8501](http://localhost:8501)
- **Database PostgreSQL**: Berjalan di `localhost:5432`

### 3. Perintah Operasional Docker

| Kebutuhan | Perintah |
| :--- | :--- |
| **Melihat Log Aplikasi** | `docker compose logs -f web` |
| **Melihat Log Database** | `docker compose logs -f db` |
| **Menghentikan Layanan** | `docker compose down` |
| **Restart Layanan** | `docker compose restart` |
| **Migrasi Skema & Seed Seluruh Data** | `docker compose exec web python -m database.migrate_and_seed` |
| **Inisialisasi / Seed User Saja** | `docker compose exec web python -m database.seed_users` |
| **Menjalankan ETL Presensi ke PostgreSQL** | `docker compose exec web python -m etl.presensi_etl` |
| **Menjalankan Unit Test** | `docker compose exec web pytest -q` |

---

## 🔑 Kredensial Login Sistem (PostgreSQL Auth)

Tabel pengguna (`users`) dibuat dan di-seed otomatis saat container aplikasi pertama kali berjalan. Akun default yang tersedia:

| Role | Username | Password | Deskripsi |
| :--- | :--- | :--- | :--- |
| **Admin** | `admin` | `admin123` | Administrator Utama EWS |
| **Operator** | `operator` | `operator123` | Operator Presensi Unit |
| **Pimpinan** | `pimpinan` | `pimpinan123` | Pimpinan Eksekutif / Kepala |

### 👥 Modul Manajemen Pengguna
Aplikasi dilengkapi dengan modul **Manajemen Pengguna** (`modules/user_management.py`) yang terhubung langsung ke PostgreSQL untuk:
- Menampilkan metrik & daftar seluruh akun pengguna terdaftar.
- Menambah pengguna baru dengan pilihan peran (`admin`, `operator`, `pimpinan`).
- Mengubah profil, status aktif/nonaktif, dan reset kata sandi.
- Menghapus pengguna dengan proteksi akun aktif tunggal.
- Seluruh riwayat perubahan tercatat otomatis pada log aktivitas audit sistem.

### 🏛️ Modul Master OPD / Dinas
Aplikasi dilengkapi dengan modul **Master Organisasi Perangkat Daerah (OPD / Dinas)** (`modules/opd_management.py`) yang menyimpan data ke tabel `opd` di PostgreSQL dengan struktur lengkap:
- `id`, `kode`, `kode_sipd`, `nama`, `singkatan`, `jenis`, `parent_id`
- `alamat`, `telepon`, `email`, `website`, `kepala_nama`, `kepala_nip`
- `aktif`, `created_at`, `updated_at`, `deleted_at` (soft delete & restore)

### 👥 Modul Master Data Pegawai & Import Excel
Aplikasi dilengkapi dengan modul **Master Data Pegawai** (`modules/pegawai_management.py`) yang menyimpan data ke tabel `pegawai` di PostgreSQL dengan struktur:
- `id_pegawai`, `nip`, `nama_pegawai`, `id_opd` (Foreign Key ke `opd.id`)
- `jabatan`, `jenis_kelamin`, `status_pegawai` (`PNS`, `PPPK`, `NON-ASN`, dll.)
- `aktif`, `created_at`, `updated_at`, `deleted_at`
- **Fitur Import Excel**: Unduh template Excel contoh, upload spreadsheet `.xlsx`/`.csv`, dan sistem otomatis melakukan sinkronisasi/UPSERT berbasis NIP ke PostgreSQL.

### 📋 Modul Data Presensi & Master Periode
Aplikasi dilengkapi dengan modul **Data Presensi & Master Periode** (`modules/presensi_data_management.py`) yang menyimpan data ke tabel PostgreSQL:
- **Tabel `periode`**:
  - `id_periode`, `bulan`, `tahun`, `tanggal_mulai`, `tanggal_selesai`, `keterangan`
- **Tabel `presensi`**:
  - `id_presensi`, `id_pegawai` (FK ke `pegawai.id_pegawai`), `id_periode` (FK ke `periode.id_periode`)
  - `tanggal_presensi`, `jam_masuk`, `jam_pulang`, `status_presensi`
  - `keterlambatan_menit`, `sumber_data`, `waktu_insert`, `waktu_update`

---

## 🔄 Migrasi Skema & Seeding Data PostgreSQL

Aplikasi menyediakan script master migrasi dan seed terintegrasi ([database/migrate_and_seed.py](file:///Users/pakdik/www/tugas-akhir-aida/database/migrate_and_seed.py)) yang bersifat **idempotent** (aman dijalankan berulang kali).

Ketika developer lain melakukan `git pull` atau menjalankan container untuk pertama kali, cukup jalankan:

```bash
docker compose exec web python -m database.migrate_and_seed
```

### Apa yang Dilakukan Script Ini?
1. **Membuat Seluruh Skema Tabel**: `users`, `opd`, `pegawai`, `periode`, `presensi`, dan `presensi_harian`.
2. **Seed Akun Pengguna Default**: `admin`, `operator`, dan `pimpinan`.
3. **Seed Master OPD / Dinas**: 11 OPD lengkap (BAPPEDA, BKAD, BKD, DISKOMINFO, INSPEKTORAT, ROHUKUM, ROORGANISASI, BKPSDM, DISDIK, DINKES, SETDA).
4. **Seed Master Periode**: 12 Periode Bulanan Tahun 2026 (Januari s/d Desember).
5. **Migrasi Master Pegawai**: Otomatis mengekstrak seluruh pegawai unik dari 56 file Excel presensi di folder `data/` dan menghubungkannya dengan OPD masing-masing.
6. **Migrasi Riwayat Presensi**: Menyinkronkan seluruh puluhan ribu baris riwayat presensi ke tabel `presensi_harian` dan tabel relasional `presensi`.

---

## 📊 Konfigurasi Environment Variable (`.env`)

Pengaturan database PostgreSQL dan aplikasi dapat dikustomisasi langsung melalui file [`.env`](file:///.env) (disalin dari [`.env.example`](file:///.env.example)):

```dotenv
# Kredensial Database PostgreSQL
POSTGRES_USER=myuser
POSTGRES_PASSWORD=mysecretpassword
POSTGRES_DB=mydb
POSTGRES_PORT=5432

# Port Web Dashboard Streamlit
WEB_PORT=8501

# URL Koneksi PostgreSQL
DATABASE_URL=postgresql://myuser:mysecretpassword@db:5432/mydb
```

Variabel di atas otomatis dibaca oleh `docker-compose.yml` maupun modul Python via `python-dotenv`.

- **Sinkronisasi Data Excel ke DB**: Untuk mengisi/memperbarui data presensi dari file Excel di `data/` ke database PostgreSQL, jalankan:
  ```bash
  docker compose exec web python -m etl.presensi_etl
  ```
- ETL menggunakan metode **UPSERT** dengan constraint unik `(NIP, Tanggal)`.

---

## 📈 Logika Analitik & EWS

### 1. Tingkat Kepatuhan Presensi
```text
Tingkat Kepatuhan = (Hari Kerja - TK) / Hari Kerja × 100%
```
*Catatan: Kehadiran sah seperti Cuti, WFH, dan Dinas Luar (DL) tidak mengurangi persentase kepatuhan.*

### 2. Kategori Status EWS
- 🟢 **Normal**: 0 hari TK
- 🟡 **Perlu Perhatian**: 1–2 hari TK
- 🟠 **Perlu Verifikasi**: 3–5 hari TK
- 🔴 **Prioritas Tindak Lanjut**: ≥ 6 hari TK

*Keterlambatan menjadi indikator tambahan. Indikasi PP 94/2021 dihitung sebagai bahan pendukung evaluasi disiplin PNS setelah verifikasi di Action Center.*

---

## 🧪 Pengujian / Testing

Jalankan seluruh test suite di dalam container Docker:

```bash
docker compose exec web pytest -v
```
