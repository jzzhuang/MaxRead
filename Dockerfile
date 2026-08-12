FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MAXREAD_DATA_DIR=/app/data

RUN apt-get update \
    && apt-get install --no-install-recommends -y nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system maxread \
    && useradd --system --gid maxread --create-home maxread

WORKDIR /app
COPY requirements.txt requirements.lock package.json package-lock.json ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && npm ci --omit=dev \
    && npm cache clean --force

COPY --chown=maxread:maxread . .
RUN mkdir -p /app/data && chown -R maxread:maxread /app/data

USER maxread
CMD ["python", "MaxRead.py"]
