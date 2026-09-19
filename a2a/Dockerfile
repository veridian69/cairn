# Build with the a2a/ directory as the context. Cairn's root context deliberately
# excludes this component, its state and its credentials.
ARG GO_IMAGE=golang:1.25.0-bookworm@sha256:81dc45d05a7444ead8c92a389621fafabc8e40f8fd1a19d7e5df14e61e98bc1a
ARG RUNTIME_IMAGE=gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
FROM ${GO_IMAGE} AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/a2a . \
    && mkdir -p /out/garden/data /out/garden/run

FROM ${RUNTIME_IMAGE}
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Cairn Garden" \
      org.opencontainers.image.source="https://github.com/veridian69/cairn" \
      org.opencontainers.image.revision="${REVISION}"
COPY --from=builder /out/a2a /usr/local/bin/a2a
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/a2a/
COPY THIRD_PARTY_LICENSES /usr/share/doc/a2a/THIRD_PARTY_LICENSES
COPY --from=builder --chown=65532:65532 /out/garden /var/lib/garden
USER 65532:65532
WORKDIR /var/lib/garden
EXPOSE 9443
ENTRYPOINT ["/usr/local/bin/a2a", "host", "--config", "/etc/garden/host.json"]
