# Dashy is stdlib-only Python, so there is nothing to install and nothing to
# compile - the image is the stock interpreter with the app copied in.
FROM python:3.13-alpine

LABEL org.opencontainers.image.title="Dashy" \
      org.opencontainers.image.description="A self-discovering dashboard for TrueNAS SCALE" \
      org.opencontainers.image.licenses="MIT"

COPY app/ /app/
RUN chmod +x /app/entrypoint.sh /app/collect.sh /app/worker.sh /app/serve_setup.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    DASH_DATA=/data \
    DASH_QUEUE=/queue \
    DASH_CONFIG=/config

WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-u", "/app/server.py"]
