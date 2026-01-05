FROM python:3.12-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY certs/sectigo.crt /usr/local/share/ca-certificates/sectigo.crt

RUN update-ca-certificates

RUN pip install --no-cache-dir requests

WORKDIR /app
COPY main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV STATE_FILE=/data/wsp_reg_state.json

ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN mkdir -p /data
CMD ["python", "/app/main.py"]
