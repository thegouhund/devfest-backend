FROM python:3.12-slim

WORKDIR /app

# libgomp1 dibutuhkan scipy/numpy yang dipakai open-rppg. Paket lainnya
# untuk opencv-python-headless: meski "headless", build-nya masih
# ditautkan (link) ke sejumlah shared library grafis dasar saat startup,
# walau tidak dipakai untuk GUI apa pun. Ketauan satu-satu lewat error
# "cannot open shared object file" (libxcb.so.1, lalu libGL.so.1) — daftar
# di bawah adalah dependency umum opencv-python-headless di base image
# minimal (Debian slim), disatukan sekaligus supaya tidak berulang lagi.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        libxcb1 \
        libgl1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

# Video wajah ditulis ke sini; di-mount sebagai volume supaya tidak ikut
# terhapus saat container dibangun ulang.
RUN mkdir -p /data/videos && chmod 700 /data/videos

EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
