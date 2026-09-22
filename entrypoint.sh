#!/bin/bash
# Converts the mounted Docker secrets into the forms Odoo and libpq expect, then
# hands over to Odoo. Secrets only ever land on the tmpfs at /tmp.
set -eo pipefail
umask 077

# Fail loudly on a missing secret. Silently continuing would write an empty
# password into .pgpass (libpq then reports a bare authentication failure) and
# export an empty ODOO_ADMIN_PASSWD, which config.verify_admin_password treats
# as "authentication forbidden" -- both a long way from the real cause.
: "${PGPASSWORD_FILE:?must point at a mounted secret}"
: "${ODOO_ADMIN_PASSWD_FILE:?must point at a mounted secret}"
for secret in "$PGPASSWORD_FILE" "$ODOO_ADMIN_PASSWD_FILE"; do
    if [ ! -r "$secret" ]; then
        echo "entrypoint: cannot read secret $secret" >&2
        exit 1
    fi
done

# Odoo 19 reads PGHOST/PGPORT/PGUSER itself (tools/config.py registers them as
# env_name on --db_host/--db_port/--db_user), so compose sets them directly and
# nothing here needs to translate them into --db_* flags.

# db_password is left unset, so Odoo omits it and libpq falls back to PGPASSFILE.
# That keeps the password off the command line and out of `docker inspect`.
# ':' and '\' are field separators in .pgpass and have to be escaped.
PGPASSFILE=/tmp/.pgpass
printf '*:*:*:*:%s\n' \
    "$(printf '%s' "$(< "$PGPASSWORD_FILE")" | sed 's/[\\:]/\\&/g')" > "$PGPASSFILE"
export PGPASSFILE

# admin_passwd is file-only on the CLI, but tools/config.py auto-derives the env
# name ODOO_ADMIN_PASSWD for it, and env options outrank the config file.
# So config/odoo.conf never has to hold the master password.
export ODOO_ADMIN_PASSWD="$(< "$ODOO_ADMIN_PASSWD_FILE")"

# Bare flags are treated as arguments to odoo; anything else runs as given.
if [ "$#" -eq 0 ]; then set -- odoo; fi
case "$1" in
    --) shift ;&
    -*) set -- odoo "$@" ;;
esac
exec "$@"
