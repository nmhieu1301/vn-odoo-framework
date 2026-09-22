# Odoo 19 — local dev stack

Runs Odoo 19.0 CE **from the source tree in `./odoo`**, not from a published image.
The image only carries system and Python dependencies; the source is bind-mounted,
so editing it needs no rebuild.

## Layout

```
odoo-19.0/
├── .env                  # user, db name, ports, UID/GID — no passwords
├── secrets/              # one file per password, mounted as Docker secrets
├── odoo/                 # Odoo 19.0 CE source → /opt/odoo (bind mount)
├── addons/               # → /mnt/extra-addons (bind mount)
│   ├── custom-addons/                     # own modules
│   ├── vn-odoo-framework/           # VN localisation framework
│   └── oca/{account-financial-tools,reporting-engine}
├── config/odoo.conf      # → /etc/odoo (bind mount)
├── Dockerfile            # deps only: apt packages, wkhtmltopdf, rtlcss
├── entrypoint.sh
├── scripts/init-secrets.sh
└── docker-compose.yml
```

Getting the source, if `./odoo` is empty:

```bash
git clone --depth 1 -b 19.0 https://github.com/odoo/odoo.git odoo
```

Creating the secrets, if `./secrets` is empty (it is gitignored):

```bash
./scripts/init-secrets.sh
```

## Configuration flow

`.env` sets non-secret values. Compose passes them as `PGHOST` / `PGPORT` / `PGUSER`,
which Odoo 19 reads natively — no translation layer.

Passwords never pass through `.env` or `odoo.conf`. Each is a file in `secrets/`,
mounted at `/run/secrets/`, and `entrypoint.sh` converts it into the form each
consumer expects:

| Secret | Consumer | How it is delivered |
|---|---|---|
| `postgres_password` | postgres | `POSTGRES_PASSWORD_FILE` |
| `postgres_password` | odoo | `.pgpass` written to tmpfs, read by libpq via `PGPASSFILE` |
| `odoo_admin_passwd` | odoo | exported as `ODOO_ADMIN_PASSWD`, which outranks the config file |
| `pgadmin_password` | pgadmin | `PGADMIN_DEFAULT_PASSWORD_FILE` |

The database password therefore never appears in `docker inspect`, in the
process environment, or in a config file. The master password is exported into
the Odoo process only, so it stays out of `docker inspect` and out of
`config/odoo.conf`.

### About Docker secrets in plain Compose

Outside Swarm, `secrets:` is just a bind mount of a host file into `/run/secrets/`,
keeping the host file's owner and mode. The `uid` / `gid` / `mode` fields in a
secret definition are ignored.

The three services run as different UIDs (postgres 999, odoo 1000, pgadmin 5050),
so the files must be world-readable. Access is restricted at the directory level
instead — 0700 on `secrets/`, 0644 on the files. `scripts/init-secrets.sh`
applies that and is safe to re-run.

### UID / GID

`UID` / `GID` in `.env` are build args. The container's `odoo` user is created with
them so it matches the host account owning `./odoo`, `./addons` and `./secrets`.
Check with `id -u` / `id -g`; if they differ from 1000, update `.env` and run
`docker compose up -d --build`. The image tag carries the UID
(`odoo19-web:uid1000`) so a changed UID cannot silently reuse the old image.

Note `UID` is a read-only shell variable in bash. If your shell exports it, the
exported value wins over `.env`.

### POSTGRES_DB

`POSTGRES_DB=odoo19-demo` makes postgres create an **empty** database of that name
when the volume is first initialised. That is a postgres database, not yet an Odoo
one — it only shows in the database picker after `-i base` has run. Since it already
exists, the web "Create database" button reports a name clash; use the CLI.

## Run

```bash
docker compose up -d --build
docker compose logs -f web
```

| Service | URL | Notes |
|---|---|---|
| web (odoo) | http://localhost:8069 | `admin` / `admin`, after the database is initialised |
| db (postgres 16) | not published | reachable internally as `db` |
| pgadmin | http://localhost:5050 | `PGADMIN_EMAIL` / `secrets/pgadmin_password` |

`POSTGRES_DB` creates the `odoo19-demo` database, but postgres creates it empty —
Odoo has to initialise it once before the web UI can serve it:

```bash
docker compose run --rm web odoo -d odoo19-demo -i base --stop-after-init
docker compose restart web
```

Add `--without-demo=all` to skip demo data. Then open http://localhost:8069 and
log in as `admin` / `admin`. The master password (create/drop/backup a database)
is `secrets/odoo_admin_passwd`.

To start over:

```bash
docker compose exec db dropdb -U odoo odoo19-demo
docker compose exec db createdb -U odoo -O odoo odoo19-demo
docker compose run --rm web odoo -d odoo19-demo -i base --stop-after-init
docker compose restart web
```

## Working on addons

`addons/` is a container of addon groups, not of modules. Each directory that
holds modules directly is its own `addons_path` entry in `config/odoo.conf`, so
the OCA repos are listed individually:

```
/mnt/extra-addons/custom-addons
/mnt/extra-addons/vn-odoo-framework
/mnt/extra-addons/oca/account-financial-tools
/mnt/extra-addons/oca/reporting-engine
```

Adding another group (a new OCA repo, say) means adding a line there and
restarting `web`. Odoo does not expand globs in `addons_path`.

Two modules are present but not installable, because their dependencies are not
vendored: `report_async` needs `queue_job` (OCA/queue) and `kpi` needs
`base_external_dbsource` (OCA/server-tools). Two more need Python packages the
image does not carry: `account_payroll_sheet_import` needs `pandas`, and
`report_py3o` needs `py3o.template` / `py3o.formats`. Everything else in the
51 discovered modules resolves.

```bash
# install a module
docker compose run --rm web odoo -d odoo19-demo -i omux_backend --stop-after-init

# update after editing python/xml
docker compose run --rm web odoo -d odoo19-demo -u omux_backend --stop-after-init

# update everything
docker compose run --rm web odoo -d odoo19-demo -u all --stop-after-init
```

Edits under `addons/` and `odoo/` are visible in the container immediately.
`.py` / `.xml` changes still need `-u`; `.js` / `.scss` only need an asset cache
flush (Ctrl+F5, or enable developer mode).

Auto-reload on python changes (requires `workers = 0`, already set):

```bash
docker compose run --rm --service-ports web odoo -d odoo19-demo --dev=xml,reload,qweb
```

## Common commands

```bash
docker compose ps
docker compose restart web
docker compose down              # stop, KEEP data
docker compose down -v           # stop and WIPE database + filestore
docker compose exec db psql -U odoo -d postgres -c '\l'
docker compose exec web bash
```

## Changing passwords

`secrets/postgres_password` only takes effect when the db volume is first
initialised. Afterwards, pick one:

```bash
# 1: change it in the database to match the file
docker compose exec db psql -U odoo -d postgres -c "ALTER USER odoo WITH PASSWORD 'new_password';"

# 2: wipe and start over (loses all data)
docker compose down -v && docker compose up -d
```

`secrets/odoo_admin_passwd` and `secrets/pgadmin_password` apply on restart:

```bash
docker compose up -d --force-recreate web pgadmin
```

## Rebuilding the image

Only needed when a dependency changes — never for Odoo source or addon edits.

```bash
docker compose build web && docker compose up -d
```

The apt index and downloaded `.deb`s live in BuildKit cache mounts, so a rebuild
does not refetch them. Use `--no-cache` only to force a genuine dependency refresh.

`config/odoo.conf` is not baked into the image — compose mounts `./config` over
`/etc/odoo`, so the host copy is the only one.

## pgAdmin

Log in with `PGADMIN_EMAIL` from `.env` and the contents of
`secrets/pgadmin_password`. Both are only read while the `pgadmin-data` volume is
empty — once pgAdmin has initialised it, the credentials live in that volume and
changing the secret has no effect.

Add the server manually:

| Field | Value |
|---|---|
| Host | `db` |
| Port | `5432` |
| Username | `POSTGRES_USER` from `.env` |
| Password | contents of `secrets/postgres_password` |

Reset pgAdmin to a blank state (loses all saved servers, re-reads the credentials):

```bash
docker compose stop pgadmin && docker volume rm odoo19_pgadmin-data && docker compose up -d pgadmin
```
