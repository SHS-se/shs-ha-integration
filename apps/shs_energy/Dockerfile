ARG TARGETARCH
FROM node:24.14.0-bookworm-slim AS frontend
WORKDIR /src/web
COPY web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.13.12-slim-bookworm AS companion
WORKDIR /src
COPY app/ ./app/
COPY scripts/build-companion.py ./scripts/build-companion.py
COPY custom_components/ ./custom_components/
RUN python scripts/build-companion.py /bundle
COPY web/package.json /bundle/app.json

FROM python:3.13.12-slim-bookworm AS runtime
ARG BUILD_VERSION=0.1.0-beta.24
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
