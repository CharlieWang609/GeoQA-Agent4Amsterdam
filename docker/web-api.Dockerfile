# SPDX-License-Identifier: GPL-3.0-only
#
# Web/API image: stage 1 builds the React bundle, stage 2 installs the
# default pixi environment (GeoPandas execution runs in process) and serves
# both the API and the static frontend from one container.

FROM node:22-bookworm-slim AS web-build

WORKDIR /workspace/app/web
COPY app/web/package.json app/web/package-lock.json ./
RUN npm ci
COPY app/web/ ./
RUN npm run build

FROM ghcr.io/prefix-dev/pixi:0.75.0

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY pixi.toml pixi.lock pyproject.toml LICENSE /workspace/
COPY app /workspace/app
COPY evaluation /workspace/evaluation
COPY ontology /workspace/ontology
COPY scripts /workspace/scripts
COPY src /workspace/src
RUN pixi install --environment default --locked
COPY --from=web-build /workspace/app/web/dist /workspace/app/web/dist

ARG GEOQA_CODE_COMMIT
ENV GEOQA_CODE_COMMIT=${GEOQA_CODE_COMMIT}
ENV GEOQA_STATIC_DIR=/workspace/app/web/dist
ENV PORT=8000
EXPOSE 8000
ENTRYPOINT ["/workspace/.pixi/envs/default/bin/python", "scripts/run_cloud_sandbox.py"]
