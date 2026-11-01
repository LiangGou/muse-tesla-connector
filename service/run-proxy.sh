#!/bin/bash
# Broker-supervised tesla-http-proxy: loopback only, local Fleet API signing
# key, own self-signed TLS cert. The tesla-http-proxy binary comes from
# Tesla's official vehicle-command repository — build or download it from
# there and point TESLA_HTTP_PROXY_BIN at it (or put it on your PATH).
set -e
SERVICE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROXY_DIR="$SERVICE_DIR/proxy"
mkdir -p "$PROXY_DIR"
cd "$PROXY_DIR"
if [ ! -f tls-cert.pem ]; then
  openssl req -x509 -nodes -newkey ec \
    -pkeyopt ec_paramgen_curve:secp384r1 -pkeyopt ec_param_enc:named_curve \
    -subj '/CN=localhost' -keyout tls-key.pem -out tls-cert.pem -sha256 -days 3650 \
    -addext "subjectAltName = IP:127.0.0.1, DNS:localhost" \
    -addext "extendedKeyUsage = serverAuth" \
    -addext "keyUsage = digitalSignature, keyCertSign, keyAgreement" 2>/dev/null
  chmod 600 tls-key.pem
fi
BIN=${TESLA_HTTP_PROXY_BIN:-tesla-http-proxy}
exec "$BIN" \
  -tls-key tls-key.pem -cert tls-cert.pem \
  -key-file "$SERVICE_DIR/keys/tesla-fleet-private-key.pem" \
  -port 4443 -host 127.0.0.1
