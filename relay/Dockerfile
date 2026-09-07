FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /relay ./cmd/relay

# No outbound TLS calls (the relay never talks to Cloudflare or the auth
# service directly, only proxies over an already-established local
# connection), so no CA bundle is needed -- a bare scratch image is enough.
FROM scratch
COPY --from=build /relay /relay
EXPOSE 8600
ENTRYPOINT ["/relay"]
