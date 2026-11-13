FROM python:3.12-slim

LABEL org.opencontainers.image.title="Quest for Earth"
LABEL org.opencontainers.image.description="A browser multiplayer space-scavenger RPG: the bugs took the solar system; live off what you kill, steal or scavenge"
LABEL org.opencontainers.image.source="https://github.com/glenngl/quest-for-earth"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

CMD ["quest-for-earth"]
