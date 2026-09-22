# syntax=docker/dockerfile:1

# Runtime image for Odoo 19.0 run from local source.
# The image carries only system + Python dependencies; the Odoo source itself is
# bind-mounted from ./odoo at runtime so edits need no rebuild.

FROM ubuntu:noble

LABEL org.opencontainers.image.title="odoo-19.0-dev" \
      org.opencontainers.image.description="Odoo 19.0 runtime deps; source bind-mounted from ./odoo"

SHELL ["/bin/bash", "-eo", "pipefail", "-c"]

ENV LANG=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Let BuildKit cache mounts hold the apt index and .debs across builds.
RUN rm -f /etc/apt/apt.conf.d/docker-clean

# Python and system dependencies: odoo/debian/control's runtime Depends, plus
# the extras Odoo imports without declaring (odf, pdfminer, phonenumbers,
# slugify, watchdog, xlwt) and python3-ldap for the LDAP module.
# Ubuntu 24.04 is where Odoo 19 pins its supported versions, so apt gives the
# right ones without compiling lxml / gevent / python-ldap from source.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-dejavu-core \
        fonts-font-awesome \
        fonts-inconsolata \
        fonts-noto-cjk \
        fonts-roboto-unhinted \
        gsfonts \
        python3 \
        python3-asn1crypto \
        python3-babel \
        python3-cbor2 \
        python3-chardet \
        python3-cryptography \
        python3-dateutil \
        python3-docutils \
        python3-freezegun \
        python3-geoip2 \
        python3-gevent \
        python3-greenlet \
        python3-idna \
        python3-jinja2 \
        python3-ldap \
        python3-libsass \
        python3-lxml \
        python3-lxml-html-clean \
        python3-magic \
        python3-markupsafe \
        python3-num2words \
        python3-odf \
        python3-ofxparse \
        python3-openpyxl \
        python3-openssl \
        python3-passlib \
        python3-pdfminer \
        python3-phonenumbers \
        python3-pil \
        python3-polib \
        python3-psutil \
        python3-psycopg2 \
        python3-pypdf2 \
        python3-qrcode \
        python3-reportlab \
        python3-requests \
        python3-rjsmin \
        python3-slugify \
        python3-stdnum \
        python3-tz \
        python3-urllib3 \
        python3-vobject \
        python3-watchdog \
        python3-werkzeug \
        python3-xlrd \
        python3-xlsxwriter \
        python3-xlwt \
        python3-zeep

# rtlcss, used by Odoo to generate right-to-left stylesheets.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm && \
    npm install -g rtlcss && \
    npm cache clean --force && \
    apt-get purge --autoremove -y npm

# wkhtmltopdf with patched Qt, required for PDF reports. Checksum-pinned.
ARG TARGETARCH
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    if [ -z "${TARGETARCH}" ]; then TARGETARCH="$(dpkg --print-architecture)"; fi; \
    WKHTMLTOPDF_ARCH=${TARGETARCH}; \
    case ${TARGETARCH} in \
      "amd64") WKHTMLTOPDF_SHA=967390a759707337b46d1c02452e2bb6b2dc6d59 ;; \
      "arm64") WKHTMLTOPDF_SHA=90f6e69896d51ef77339d3f3a20f8582bdf496cc ;; \
      "ppc64le" | "ppc64el") WKHTMLTOPDF_ARCH=ppc64el && WKHTMLTOPDF_SHA=5312d7d34a25b321282929df82e3574319aed25c ;; \
    esac; \
    curl -o wkhtmltox.deb -sSL "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_${WKHTMLTOPDF_ARCH}.deb" && \
    echo "${WKHTMLTOPDF_SHA} wkhtmltox.deb" | sha1sum -c - && \
    apt-get update && \
    apt-get install -y --no-install-recommends ./wkhtmltox.deb && \
    rm -f wkhtmltox.deb

# Run as a non-root user whose UID/GID match the host account that owns ./odoo,
# ./addons and ./secrets. Without this the bind-mounted files are unreadable or
# unwritable from inside the container.
ARG UID=1000
ARG GID=1000
RUN userdel -r ubuntu 2>/dev/null || true; \
    groupadd -g "${GID}" odoo 2>/dev/null || true; \
    useradd -u "${UID}" -g "${GID}" -d /var/lib/odoo -m -s /bin/bash odoo

# /opt/odoo is the bind-mount target for the local source tree.
ENV PYTHONPATH=/opt/odoo \
    ODOO_RC=/etc/odoo/odoo.conf \
    PYTHONPYCACHEPREFIX=/var/lib/odoo/pycache

COPY --chmod=0755 entrypoint.sh /usr/local/bin/entrypoint.sh

# `odoo ...` resolves to the bind-mounted source, so every documented command
# works unchanged. Dangling at build time; the mount supplies the target.
RUN ln -s /opt/odoo/odoo-bin /usr/local/bin/odoo

# 8069 http, 8072 websocket (only served when workers > 0).
EXPOSE 8069 8072

USER odoo
WORKDIR /opt/odoo

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["odoo"]
