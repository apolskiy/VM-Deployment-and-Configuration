# One-shot seeder: writes the encrypted inventory the service reads. Reuses the
# project's own inventory code so the container datastore is byte-compatible with
# the VM path and the Go service.
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "cryptography>=46.0.0"
COPY src/ /app/src/
COPY docker/seeder/seed.py /app/seed.py
ENTRYPOINT ["python", "/app/seed.py"]
