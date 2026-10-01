struktur 
tugas-akhir-aida/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── app.py
│
├── database/
│   ├── __init__.py
│   ├── connection.py
│   ├── schema.py
│   ├── auth.py
│   ├── opd.py
│   ├── pegawai.py
│   ├── periode.py
│   ├── presensi.py
│   ├── repository.py
│   ├── seed_users.py
│   └── migrate_and_seed.py
│
├── modules/
│   ├── __init__.py
│   ├── analytics.py
│   ├── attendance_compliance.py
│   ├── attendance_indicators.py
│   ├── attendance_trend.py
│   ├── discipline_rules.py
│   ├── display.py
│   ├── employee_active_status.py
│   ├── employee_type.py
│   ├── excel_parser.py
│   ├── monitoring_rules.py
│   ├── opd_management.py
│   ├── pegawai_management.py
│   ├── pp94.py
│   ├── presensi_data_management.py
│   ├── tk_report.py
│   └── user_management.py
│
├── services/
│   ├── __init__.py
│   ├── activity_log.py
│   ├── data_source.py
│   ├── epresensi.py
│   └── work_calendar.py
│
├── etl/
│   ├── __init__.py
│   └── presensi_etl.py
│
├── data/
│   ├── Rekap_Bulanan_FormatPDF_*.xlsx
│   ├── action_center_state.json
│   ├── nonaktif_pegawai.csv
│   ├── audit_logs/
│   │   └── activity_log.jsonl
│   ├── dokumen_resmi/
│   │   └── 2026/SE_Cuti_Bersama_2026.pdf
│   └── kalender/
│       ├── kalender_kerja_2026.csv
│       └── cache/
│
└── tests/
    └── test_*.py (27 file test)
#### 1. File Root & Konfigurasi Lingkungan

-   Dockerfile: Mendefinisikan konfigurasi image Docker berbasis  `python:3.10-slim`, menginstal dependensi sistem (driver PostgreSQL  `libpq-dev`, engine OCR  `tesseract-ocr`  untuk parsing kalender,  `build-essential`), menginstal library Python, dan menjalankan server Streamlit pada port  `8501`.
-   docker-compose.yml: Mengatur orkestrasi multi-container untuk layanan database  **PostgreSQL 15**  (`postgres_db`) dan aplikasi web  **Streamlit**  (`streamlit_app`), termasuk volume persistensi database (`postgres_data`) dan jaringan bridge lokal.
-   requirements.txt: Daftar dependensi Python utama proyek (Streamlit, Pandas, SQLAlchemy, psycopg2-binary, ReportLab, OpenPyXL, PyPDF, PyMuPDF, PyTesseract, dan Requests).
-   README.md: Dokumentasi resmi panduan setup proyek, instruksi perintah Docker, kredensial login default, dan arsitektur database.
-   app.py:  **Entry point utama aplikasi Streamlit**. Mengatur inisialisasi sesi, form login multi-role (Admin, Operator, Pimpinan), layout sidebar navigasi, rendering metrik eksekutif, filter dinamis (periode, OPD, status kepegawaian), grafik analitik, hingga routing ke modul-modul manajemen data.

----------

#### 2. Direktori  `database/`  (Data Persistence & Schema Layer)

-   database/**init**.py: Inisialisasi package layer database.
-   database/connection.py: Mengelola koneksi database PostgreSQL menggunakan SQLAlchemy Engine ter-cache (`get_engine`), serta otomatis membuat database tujuan (`ensure_database_exists`) jika belum tersedia.
-   database/schema.py: Menyediakan objek  `MetaData()`  tunggal yang dibagikan ke seluruh definisi tabel SQLAlchemy.
-   database/auth.py: Mengelola skema tabel  `users`, enkripsi hashing password dengan salt (SHA-256), autentikasi verifikasi login, dan operasi CRUD akun pengguna.
-   database/opd.py: Mengelola skema tabel  `opd`  (Master Dinas/OPD), fungsi CRUD, soft delete & restore, serta seeding daftar OPD default.
-   database/pegawai.py: Mengelola skema tabel  `pegawai`, normalisasi data ASN (NIP, jenis kelamin, status PNS/PPPK), CRUD pegawai, dan integrasi impor data dari file Excel.
-   database/periode.py: Mengelola skema tabel  `periode`  dan fungsi pencarian/pembuatan otomatis periode presensi bulanan.
-   database/presensi.py: Mengelola skema tabel relasional  `presensi`  (terhubung ke tabel  `pegawai`  dan  `periode`), operasi insert batch, input manual, dan query data presensi.
-   database/repository.py: Mengelola tabel  `presensi_harian`  (skema flat untuk query cepat), logika validasi data, mekanisme UPSERT harian, dan query data dengan filter terindeks.
-   database/seed_users.py: Script standalone untuk membuat tabel pengguna dan memasukkan akun default (`admin`,  `operator`,  `pimpinan`).
-   database/migrate_and_seed.py:  **Master script migrasi dan seeding terpadu**. Script ini bersifat  _idempotent_  untuk membuat seluruh tabel, men-seed OPD default, membuat 12 periode presensi 2026, memigrasikan master pegawai, serta menyinkronkan seluruh riwayat data presensi dari file Excel ke PostgreSQL.

----------

#### 3. Direktori  `modules/`  (Business Logic & UI Components)

-   modules/**init**.py: Inisialisasi package modul bisnis.
-   modules/analytics.py: Menghitung agregasi metrik dashboard (tingkat kehadiran, ringkasan status EWS, tren bulanan, rekap per OPD, dan rekap per pegawai).
-   modules/attendance_compliance.py: Menghitung persentase kepatuhan presensi berdasarkan hari kerja wajib dan jam kehadiran.
-   modules/attendance_indicators.py: Menentukan status kehadiran harian pegawai (hadir fisik, terlambat, pulang awal, TK, cuti, dinas luar, WFH).
-   modules/attendance_trend.py: Mengelompokkan data time-series kepatuhan presensi untuk visualisasi grafik tren harian dan bulanan.
-   modules/discipline_rules.py: Mengimplementasikan aturan penegakan disiplin ASN berdasarkan  **Pergub Kalbar No. 2 Tahun 2024**  (akumulasi TK tahunan dan hari TK berturut-turut).
-   modules/pp94.py: Mengimplementasikan logika evaluasi pelanggaran disiplin dan penentuan tingkat hukuman disiplin berdasarkan  **PP No. 94 Tahun 2021**.
-   modules/monitoring_rules.py: Menghitung ambang batas Early Warning System (EWS) untuk mendeteksi pegawai dengan indikasi rawan disiplin.
-   modules/excel_parser.py: Mengekstrak dan membersihkan data dari puluhan file spreadsheet Excel rekap presensi bulanan BKD ke struktur DataFrame canonical.
-   modules/employee_type.py: Mengidentifikasi jenis pegawai (PNS, PPPK, atau Non-ASN) secara otomatis berdasarkan pola dan panjang NIP/NI PPPK.
-   modules/employee_active_status.py: Memfilter dan menyesuaikan populasi presensi pegawai yang telah pensiun, mutasi, atau non-aktif berdasarkan tanggal efektif.
-   modules/display.py: Helper pemformatan teks, angka desimal, dan persentase yang aman dari nilai  `null`/`NaN`.
-   modules/tk_report.py: Mengenerate laporan resmi Tanpa Keterangan (TK) dalam format  **PDF**  (menggunakan ReportLab) dan  **Excel**  (menggunakan OpenPyXL).
-   modules/user_management.py: Tampilan antarmuka Streamlit untuk manajemen pengguna (tambah akun, ubah role, reset password, status aktif/non-aktif).
-   modules/opd_management.py: Tampilan antarmuka Streamlit untuk manajemen Master Organisasi Perangkat Daerah (OPD) beserta soft delete dan restore.
-   modules/pegawai_management.py: Tampilan antarmuka Streamlit untuk Master Pegawai dengan fitur unduh template dan unggah massal (_batch import_) spreadsheet Excel.
-   modules/presensi_data_management.py: Tampilan antarmuka Streamlit untuk pengelolaan rekaman presensi harian dan konfigurasi periode presensi.

----------

#### 4. Direktori  `services/`  (External Services & Infrastructure)

-   services/**init**.py: Inisialisasi package layer services.
-   services/activity_log.py: Mencatat seluruh riwayat audit dan log aktivitas operasional sistem secara real-time ke file JSON Lines dengan proteksi thread-safe.
-   services/data_source.py: Layer abstraksi sumber data (_data source switcher_) yang menghubungkan aplikasi ke PostgreSQL atau fallback ke Excel secara transparan.
-   services/epresensi.py: Service client HTTP untuk integrasi dan sinkronisasi data presensi langsung dari portal eksternal e-Presensi Pemprov Kalbar.
-   services/work_calendar.py: Mengelola kalender kerja resmi tahun berjalan (membedakan hari kerja efektif, hari libur nasional, dan cuti bersama) berdasarkan data Surat Edaran resmi.

----------

#### 5. Direktori  `etl/`  (Extract, Transform, Load)

-   etl/**init**.py: Inisialisasi package ETL.
-   etl/presensi_etl.py: Script pipeline otomatis untuk membaca seluruh rekap Excel presensi di folder  `data/`, melakukan normalisasi data, dan menyimpannya langsung ke database PostgreSQL.

----------

#### 6. Direktori  `data/`  (Data Storage & Dokumen Resmi)

-   `data/Rekap_Bulanan_FormatPDF_*.xlsx`: Kumpulan 56 file Excel rekap presensi bulanan (Januari s/d Agustus 2026) dari 7 OPD/Dinas instansi pemerintah.
-   `data/action_center_state.json`: File persistensi status tindak lanjut rekomendasi EWS.
-   `data/nonaktif_pegawai.csv`: Berkas konfigurasi daftar pegawai yang non-aktif/pensiun beserta tanggal efektifnya.
-   `data/dokumen_resmi/2026/SE_Cuti_Bersama_2026.pdf`: Dokumen PDF Surat Edaran resmi mengenai penetapan hari libur dan cuti bersama tahun 2026.
-   `data/kalender/kalender_kerja_2026.csv`: Dataset tabular master kalender kerja tahun 2026.
-   `data/audit_logs/activity_log.jsonl`: File log audit pencatatan aktivitas pengguna dan operasional sistem.

----------

#### 7. Direktori  `tests/`  (Unit & Integration Testing)

Berisi 27 file pengujian otomatis berbasis  `pytest`  untuk menjamin integritas logika analitik, kepatuhan presensi, koneksi database, parser Excel, hingga komponen antarmuka Streamlit (misal:

test_auth.py,

test_pp94_verification.py,

test_excel_parser.py, dan

test_migrate_and_seed.py).