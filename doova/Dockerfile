FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
<<<<<<< HEAD
RUN pip install -r requirements.txt
=======
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install -r requirements.txt
>>>>>>> 6d6cdb9 (feat: 'generate videos')

# As fontes (Inter, OFL) estão em app/assets/fonts: nenhuma fonte do sistema é necessária.
COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 doova \
    && mkdir -p /app/data /app/generated \
    && chown -R doova:doova /app
USER doova

ENV APP_ENV=production PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1

# Um único worker: o agendador interno roda dentro do processo (as travas no banco
# protegem contra duplicidade mesmo assim).
<<<<<<< HEAD
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
=======
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
>>>>>>> 6d6cdb9 (feat: 'generate videos')
