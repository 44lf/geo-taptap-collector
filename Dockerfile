FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEO_COLLECTOR_DATA_DIR=/var/lib/geo-game-collector

RUN groupadd --system --gid 10001 collector \
    && useradd --system --uid 10001 --gid collector --home-dir /nonexistent collector

WORKDIR /opt/geo-game-collector
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN install -d -o collector -g collector /var/lib/geo-game-collector
USER collector:collector

VOLUME ["/var/lib/geo-game-collector"]
ENTRYPOINT ["geo-game-collector"]
CMD ["agent"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD ["geo-game-collector", "readiness"]
