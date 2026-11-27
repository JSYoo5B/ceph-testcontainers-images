# Clock helper for opt-in daemon clock faults (docs/EXTENSIONS.md). It is a
# file source and never runs: a module creates a container of it, copies the
# interposer and its manifest out through the engine, and places the interposer
# into a Ceph daemon container. The published image holds the libfaketime
# default build, its manifest and its license only; the compiler stays in the
# build stage, which uses the pinned Debian base of image/debian.
ARG BASE_IMAGE=debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
ARG BASE_NAME=debian:bookworm-slim

FROM ${BASE_IMAGE} AS build
ARG BASE_NAME
# libfaketime v0.9.12, the commit of the clock-probe helper. The build verifies
# that the fetched commit is exactly this one.
ARG LIBFAKETIME_TAG=v0.9.12
ARG LIBFAKETIME_COMMIT=3ccdd344aadf1e317156fa5fb7c881f2c4404778
COPY build-interposer.sh write-sources.sh /usr/local/lib/ceph-testcontainers/
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential python3 git ca-certificates \
    && LIBFAKETIME_TAG="${LIBFAKETIME_TAG}" LIBFAKETIME_COMMIT="${LIBFAKETIME_COMMIT}" OUTPUT=/out \
        sh /usr/local/lib/ceph-testcontainers/build-interposer.sh \
    && OUTPUT=/out BUILD_BASE="${BASE_NAME}" sh /usr/local/lib/ceph-testcontainers/write-sources.sh

FROM scratch
COPY --from=build /out/ /
LABEL org.opencontainers.image.source="https://github.com/JSYoo5B/ceph-testcontainers-images" \
      org.opencontainers.image.description="clock helper (the libfaketime default interposer and its build manifest) for Ceph testcontainers daemon clock faults; see /usr/share/ceph-testcontainers/SOURCES.txt for licenses and corresponding source" \
      io.ceph-testcontainers.extension="clock" \
      io.ceph-testcontainers.extension.contract="1" \
      io.ceph-testcontainers.source-notice="/usr/share/ceph-testcontainers/SOURCES.txt"
