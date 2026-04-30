FROM python:3.11-slim

WORKDIR /app

# ca-certificates required for HTTPS requests to OLX
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements-cli.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the CLI modules — PyQt6 GUI is not used in Docker
COPY olx_scraper.py otodom_scraper.py ./
COPY countries/ ./countries/

# Seen-listings file lives here; mount a named volume to persist it
ENV SEEN_DIR=/data
RUN mkdir -p /data

ENTRYPOINT ["python", "olx_scraper.py"]
