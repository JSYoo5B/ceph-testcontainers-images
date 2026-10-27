# Clock-probe helper for opt-in clock-skew scratch fixtures (docs/EXTENSIONS.md).
# It runs no Ceph component: a module starts it as a scratch container next to a
# Ceph cluster and runs its own probe in it. It carries a C compiler and two
# builds of libfaketime, one pinned commit compiled without and with
# INTERCEPT_SYSCALL, so a fixture needs neither a runner-side toolchain nor a
# network at test time. The base is the pinned Debian base of image/debian.
ARG BASE_IMAGE=debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
ARG BASE_NAME=debian:bookworm-slim

FROM ${BASE_IMAGE}
ARG BASE_NAME
# libfaketime v0.9.12. The build verifies that the fetched commit is exactly this one.
ARG LIBFAKETIME_TAG=v0.9.12
ARG LIBFAKETIME_COMMIT=3ccdd344aadf1e317156fa5fb7c881f2c4404778
COPY write-sources.sh build-libfaketime.sh clock-probe-build-info /usr/local/lib/ceph-testcontainers/
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential binutils python3 git ca-certificates \
    && LIBFAKETIME_TAG="${LIBFAKETIME_TAG}" LIBFAKETIME_COMMIT="${LIBFAKETIME_COMMIT}" \
        sh /usr/local/lib/ceph-testcontainers/build-libfaketime.sh \
    && install -m 0755 /usr/local/lib/ceph-testcontainers/clock-probe-build-info /usr/local/bin/clock-probe-build-info \
    && apt-get purge -y git ca-certificates && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL org.opencontainers.image.source="https://github.com/JSYoo5B/ceph-testcontainers-images" \
      org.opencontainers.image.description="clock-probe helper (compiler and pinned libfaketime builds) for Ceph testcontainers clock fixtures; see /usr/share/ceph-testcontainers/SOURCES.txt for licenses and corresponding source" \
      org.opencontainers.image.base.name="${BASE_NAME}" \
      io.ceph-testcontainers.extension="clock-probe" \
      io.ceph-testcontainers.extension.contract="1" \
      io.ceph-testcontainers.source-notice="/usr/share/ceph-testcontainers/SOURCES.txt"
CMD ["sleep", "infinity"]
