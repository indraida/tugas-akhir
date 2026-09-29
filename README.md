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
| **Inisialisasi / Seed User Manual** | `docker compose exec web python -m database.seed_users` |
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

---

## 📊 Konfigurasi Sumber Data

Secara default di Docker, variabel lingkungan di `docker-compose.yml` mengarah langsung ke database PostgreSQL:

```yaml
environment:
  - DATABASE_URL=postgresql://myuser:mysecretpassword@db:5432/mydb
```

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
