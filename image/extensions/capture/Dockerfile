# Packet-capture helper for opt-in capture fixtures (docs/EXTENSIONS.md). It
# runs no Ceph component: a module starts it in a target container's network
# namespace and grants NET_RAW to the helper alone. tcpdump holds cap_net_raw
# as a file capability and runs as the unprivileged tcpdump user, so it never
# needs root, a privilege drop or a change of file ownership. The base is the
# pinned Debian base of image/debian.
ARG BASE_IMAGE=debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
ARG BASE_NAME=debian:bookworm-slim

FROM ${BASE_IMAGE}
ARG BASE_NAME
COPY write-sources.sh /usr/local/lib/ceph-testcontainers/write-sources.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends tcpdump python3 libcap2-bin \
    && setcap cap_net_raw+ep "$(command -v tcpdump)" \
    && apt-get purge -y libcap2-bin && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL org.opencontainers.image.source="https://github.com/JSYoo5B/ceph-testcontainers-images" \
      org.opencontainers.image.description="packet-capture helper for Ceph testcontainers capture fixtures; see /usr/share/ceph-testcontainers/SOURCES.txt for licenses and corresponding source" \
      org.opencontainers.image.base.name="${BASE_NAME}" \
      io.ceph-testcontainers.extension="capture" \
      io.ceph-testcontainers.extension.contract="1" \
      io.ceph-testcontainers.source-notice="/usr/share/ceph-testcontainers/SOURCES.txt"
USER tcpdump
CMD ["sleep", "infinity"]
