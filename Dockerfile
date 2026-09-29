FROM python:3.10-slim

# Instal dependensi sistem yang dibutuhkan oleh psycopg2, pytesseract, dan ReportLab
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    tesseract-ocr \
    tesseract-ocr-ind \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Salin dan instal dependensi Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Salin seluruh kode aplikasi
COPY . .

# Streamlit menggunakan port 8501 secara default
EXPOSE 8501

# Jalankan Streamlit saat kontainer dimulai
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]

