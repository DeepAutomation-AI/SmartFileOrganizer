FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN python -m pip install --no-cache-dir '.[realtime]' \
    && groupadd --gid 10001 organizer \
    && useradd --uid 10001 --gid organizer --home-dir /data --no-create-home organizer \
    && mkdir -p /data/Downloads /data/Desktop /data/Organized /app/.state /app/.logs \
    && chown -R organizer:organizer /data /app/.state /app/.logs

USER organizer
ENTRYPOINT ["smartfileorganizer"]
CMD ["--config", "/app/config/default.yaml", "organize", "--mode", "dry-run"]
