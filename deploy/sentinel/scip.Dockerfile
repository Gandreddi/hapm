# Sentinel SCIP engine (ADR-0021): scip CLI plus the Python and TypeScript indexers at pinned versions.
# The scip CLI tarball is checked against the SHA-256 recorded when it was pinned.
FROM docker.io/library/node:20.18.1-bookworm-slim
ARG SCIP_VERSION=0.5.2
ARG SCIP_SHA256=49892eae309f3791b6b24b79cdfe57118fd1de8a19b8469073d7fdf54a0ab626
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 curl ca-certificates \
    && curl -sSL -o /tmp/scip.tar.gz "https://github.com/sourcegraph/scip/releases/download/v${SCIP_VERSION}/scip-linux-amd64.tar.gz" \
    && echo "${SCIP_SHA256}  /tmp/scip.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/scip.tar.gz -C /usr/local/bin scip && rm /tmp/scip.tar.gz \
    && npm install -g --no-fund --no-audit @sourcegraph/scip-python@0.6.8 @sourcegraph/scip-typescript@0.6.2 \
    && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 sentinel
USER sentinel
WORKDIR /work
