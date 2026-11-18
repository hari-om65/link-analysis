FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# libatomic1 is needed by the Node driver that ships inside the playwright package
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Headless Chromium + its system libraries, only used for JavaScript-heavy sites
RUN python -m playwright install --with-deps chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

RUN useradd --create-home appuser
USER appuser

EXPOSE 4000

# Most hosting platforms inject $PORT; default to 4000 locally.
# --forwarded-allow-ips lets the rate limiter see the real client IP behind the platform's proxy.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-4000} --proxy-headers --forwarded-allow-ips='*'"]
