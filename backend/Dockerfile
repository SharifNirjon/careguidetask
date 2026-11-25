# Pipeline worker image: Tesseract + pinned Python deps, runs as a non-root user.
FROM python:3.11.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # One OpenMP thread per Tesseract process: we parallelize with worker processes.
    OMP_THREAD_LIMIT=1 \
    PYTHONPATH=/app/backend

# tesseract-ocr pulls in the eng + osd traineddata. No libGL (opencv headless),
# no libmagic (file types are detected from magic bytes in app code).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt /app/backend/
ARG INSTALL_DEV=false
RUN pip install -r /app/backend/requirements.txt \
 && if [ "$INSTALL_DEV" = "true" ]; then pip install -r /app/backend/requirements-dev.txt; fi

COPY . /app/backend

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/storage \
 && chown -R app:app /data/storage
USER app

CMD ["celery", "-A", "app.workers.celery_app", "worker", "-Q", "ocr,default,llm", "--loglevel=INFO"]
