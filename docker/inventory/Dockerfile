# Encrypted inventory service. Multi-stage: build the static Go binary, then
# ship it on a minimal distroless base. The binary is identical in behaviour to
# the one baked into the golden VM image — same sources, same wire format.
FROM golang:1.26 AS build
WORKDIR /src
COPY goservice/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/inventory .

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/inventory /usr/local/bin/inventory
EXPOSE 5090
# Reads the key and datastore the seeder writes to the shared /data volume.
ENTRYPOINT ["/usr/local/bin/inventory", "-addr", ":5090", \
            "-key", "/data/inventory.key", "-store", "/data/inventory.enc"]
