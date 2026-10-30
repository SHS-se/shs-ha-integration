ARG TARGETARCH
FROM node:24.22.1-bookworm-slim AS frontend
WORKDIR /src/web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.13.17-slim-bookworm AS python-base
# Debian's security update distrusts both the eMudhra root and its AAA cross-sign.
# Fail the build if a mirror has not published the fixed bookworm package yet.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' ca-certificates)" ge '20250419~deb12u2' \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY scripts/check-tls-trust.py /usr/local/bin/check-tls-trust.py
RUN python /usr/local/bin/check-tls-trust.py

FROM python-base AS companion
WORKDIR /src
COPY app/ ./app/
COPY scripts/build-companion.py ./scripts/build-companion.py
COPY custom_components/ ./custom_components/
RUN python scripts/build-companion.py /bundle
COPY web/package.json /bundle/app.json

FROM python-base AS runtime
ARG BUILD_VERSION=0.1.0-beta.126
ARG TARGETARCH
LABEL io.hass.version="${BUILD_VERSION}" io.hass.type="app" \
      org.opencontainers.image.source="https://github.com/SHS-se/shs-ha-integration" \
      org.opencontainers.image.title="SHS Energy" \
      org.opencontainers.image.description="Smart Home Solutions energy dashboard"
WORKDIR /opt/shs
COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import sqlite3; sqlite3.connect(':memory:').execute('SELECT count(*) FROM dbstat')"
COPY app/shs_app ./shs_app
COPY --from=companion /bundle/core/shs_core ./shs_core
COPY --from=companion /bundle/core/shs_wire ./shs_wire
COPY --from=companion /bundle/core/manifest.json ./manifest.json
RUN python -c "import shs_core.household; import shs_core.api; import shs_core.home_host; import shs_core.controller; import shs_core.battery_runtime; import shs_app.migration_check; import shs_app.server; import shs_app.engine"
COPY --from=frontend /src/web/dist ./web
COPY --from=companion /bundle ./companion
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8099
CMD ["python", "-m", "shs_app.server"]

FROM runtime AS runtime-amd64
LABEL io.hass.arch="amd64"
FROM runtime AS runtime-arm64
LABEL io.hass.arch="aarch64"
FROM runtime-${TARGETARCH}
