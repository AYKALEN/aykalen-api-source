# Build stage: compilers only here, in case a dependency has no wheel for this platform.
FROM python:3.11-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime stage: no compilers, runs as an unprivileged user.
FROM python:3.11-slim

COPY --from=build /install /usr/local

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --chown=root:root . .
# Content hash of the copied files, shown by GET /source (see source_manifest.py).
RUN python source_manifest.py /app > /source-sha256.txt

USER app

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
