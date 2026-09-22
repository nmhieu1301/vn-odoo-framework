#!/bin/sh
# Creates any missing secret with a random value and enforces the modes the
# stack needs. Idempotent: existing secrets are left untouched.
#
# 0700 on the directory keeps other host users out. 0644 on the files is
# required because the three services run as different UIDs (postgres 999,
# odoo = your UID, pgadmin 5050) and plain Compose secrets are bind mounts
# that keep the host mode -- the uid/gid/mode fields of a secret are ignored.
set -eu

cd "$(dirname "$0")/.."
mkdir -p secrets
chmod 0700 secrets

for name in postgres_password odoo_admin_passwd pgadmin_password; do
    if [ ! -f "secrets/$name" ]; then
        openssl rand -base64 24 | tr -d '\n' > "secrets/$name"
        echo "generated secrets/$name"
    fi
    chmod 0644 "secrets/$name"
done

echo "secrets ready"
