ARG PYTHON_IMAGE=python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.0@sha256:606e70c71c852d03f611b1e56a195d08648507018a7057fab82c4974c4eae105

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
ARG VERSION=v0.1.0-rc.2
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
