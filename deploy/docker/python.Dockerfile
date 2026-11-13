# One image for every Python service; compose picks the command. Build context: repo root.
FROM docker.io/library/python:3.12.15-slim-trixie AS build
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /bin/uv
WORKDIR /src
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
COPY libs libs
COPY services services
COPY connectors connectors
RUN uv sync --frozen --no-dev --all-packages
COPY policies policies
COPY schemas schemas
COPY agents agents
COPY config config
COPY tests/fixtures tests/fixtures

FROM docker.io/library/python:3.12.15-slim-trixie
# Pick up OS security fixes released after the base image was built.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 hapm
COPY --from=build --chown=hapm /src /src
WORKDIR /src
ENV PATH="/src/.venv/bin:$PATH" \
    HAPM_POLICY_PATH=/src/policies/default.yaml \
    HAPM_SCHEMAS_DIR=/src/schemas/events \
    HAPM_AGENTS_DIR=/src/agents
USER hapm
