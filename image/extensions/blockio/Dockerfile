# User-space tools for opt-in bounded block-I/O fixtures. Kernel admission,
# privileged device access and fault ownership belong to the calling module.
ARG BASE_IMAGE=debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
ARG BASE_NAME=debian:bookworm-slim

FROM ${BASE_IMAGE}
ARG BASE_NAME
COPY write-sources.sh /usr/local/lib/ceph-testcontainers/write-sources.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 dmsetup mount util-linux \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL org.opencontainers.image.source="https://github.com/JSYoo5B/ceph-testcontainers-images" \
      org.opencontainers.image.description="block-I/O helper for Ceph testcontainers fixtures; see /usr/share/ceph-testcontainers/SOURCES.txt for licenses and corresponding source" \
      org.opencontainers.image.base.name="${BASE_NAME}" \
      io.ceph-testcontainers.extension="blockio" \
      io.ceph-testcontainers.extension.contract="1" \
      io.ceph-testcontainers.source-notice="/usr/share/ceph-testcontainers/SOURCES.txt"
CMD ["sleep", "infinity"]
