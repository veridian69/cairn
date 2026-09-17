ARG PYTHON_IMAGE=python:3.14.7-slim-trixie@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.14@sha256:1946145b8706ad9e5c0e79a513f9e324b58d5e38126bb2c8b7dbfca61febeb45

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /opt/cairn
COPY pyproject.toml uv.lock README.md LICENSE.md LICENSE-Apache-2.0.txt NOTICE.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY integrations/codex/cairn-memory/SKILL.md ./integrations/codex/cairn-memory/SKILL.md
COPY integrations/claude/cairn-memory/SKILL.md ./integrations/claude/cairn-memory/SKILL.md
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
# Dependencies are installed in the builder. The runtime needs neither pip
# (including its independently vendored dependencies) nor its ensurepip wheel.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python*/ensurepip
RUN apt-get update \
    && apt-get install --no-install-recommends --only-upgrade -y \
        bsdutils \
        gzip \
        libblkid1 \
        liblastlog2-2 \
        libmount1 \
        libpcre2-8-0 \
        libsmartcols1 \
        libsqlite3-0 \
        libssl3t64 \
        libuuid1 \
        login \
        mount \
        openssl \
        openssl-provider-legacy \
        perl-base \
        util-linux \
    && rm -rf /var/lib/apt/lists/*
ARG VERSION=v0.7.0-rc.2
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/veridian69/cairn" \
      org.opencontainers.image.documentation="https://github.com/veridian69/cairn" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"
ENV PATH="/opt/cairn/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/.cache
WORKDIR /opt/cairn
COPY --from=builder /opt/cairn/.venv /opt/cairn/.venv
COPY LICENSE.md LICENSE-Apache-2.0.txt NOTICE.md /opt/cairn/
RUN install -d -o 65532 -g 0 -m 0770 /var/lib/cairn
USER 65532:0
ENTRYPOINT ["cairn"]
CMD ["serve", "--config", "/etc/cairn/config.yaml"]
