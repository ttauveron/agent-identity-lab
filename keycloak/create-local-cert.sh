#!/bin/sh
set -eu
mkdir -p .local-certs
if [ ! -f .local-certs/key.pem ] || [ ! -f .local-certs/cert.pem ]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
    -keyout .local-certs/key.pem -out .local-certs/cert.pem \
    -subj '/CN=keycloak.localhost' -addext 'subjectAltName=DNS:keycloak.localhost' >/dev/null 2>&1
  chmod 600 .local-certs/key.pem
fi
