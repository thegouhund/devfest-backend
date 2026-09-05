FROM python:3.12-slim

WORKDIR /app

# libgomp1 dibutuhkan scipy/numpy yang dipakai open-rppg. Varian opencv
# headless tidak butuh libGL, tapi tetap butuh libxcb1 — beberapa build
# opencv-python-headless masih ditautkan (link) ke libxcb saat startup
# walau tidak dipakai untuk GUI. Tanpa ini `import cv2` gagal dengan
# "libxcb.so.1: cannot open shared object file", yang menyamar sebagai
# "open-rppg belum terpasang" kalau tidak diperiksa sampai ke traceback asli.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Video wajah ditulis ke sini; di-mount sebagai volume supaya tidak ikut
# terhapus saat container dibangun ulang.
RUN mkdir -p /data/videos && chmod 700 /data/videos

EXPOSE 3000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3000"]
