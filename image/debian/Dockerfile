# Minimal Ceph role images from the Ceph project's Debian packages. Each target
# installs only the packages its role needs (docs/IMAGE_REQUIREMENTS.md).
ARG DEBIAN_BASE=debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

FROM ${DEBIAN_BASE} AS common
ARG CEPH_PACKAGE_VERSION=20.2.4-1bookworm
ENV CEPH_PACKAGE_VERSION=${CEPH_PACKAGE_VERSION} DEBIAN_FRONTEND=noninteractive
ADD --chmod=644 --checksum=sha256:46207b03a3645e7d6f83790dc6c0fddb6bd08fd5a21843845cb1f294d96d8c3e \
    https://download.ceph.com/keys/release.asc /etc/apt/keyrings/ceph.asc
COPY write-sources.sh /usr/local/lib/ceph-testcontainers/write-sources.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && echo "deb [signed-by=/etc/apt/keyrings/ceph.asc] https://download.ceph.com/debian-20.2.4 bookworm main" \
        > /etc/apt/sources.list.d/ceph.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends "ceph-common=${CEPH_PACKAGE_VERSION}" \
        "python3-rados=${CEPH_PACKAGE_VERSION}" "python3-rbd=${CEPH_PACKAGE_VERSION}" \
        "python3-cephfs=${CEPH_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*
LABEL org.opencontainers.image.source="https://github.com/JSYoo5B/ceph-testcontainers-images" \
      org.opencontainers.image.description="Ceph role image built from Ceph Debian packages; see /usr/share/ceph-testcontainers/SOURCES.txt for licenses and corresponding source" \
      org.opencontainers.image.base.name="debian:bookworm-slim" \
      io.ceph-testcontainers.source-notice="/usr/share/ceph-testcontainers/SOURCES.txt"

FROM common AS control
RUN apt-get update && apt-get install -y --no-install-recommends "ceph-mon=${CEPH_PACKAGE_VERSION}" \
        "ceph-mgr=${CEPH_PACKAGE_VERSION}" "rbd-mirror=${CEPH_PACKAGE_VERSION}" \
        "cephfs-mirror=${CEPH_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL io.ceph-testcontainers.role="control"

FROM common AS osd
RUN apt-get update && apt-get install -y --no-install-recommends "ceph-osd=${CEPH_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL io.ceph-testcontainers.role="osd"

FROM common AS rgw
RUN apt-get update && apt-get install -y --no-install-recommends "radosgw=${CEPH_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL io.ceph-testcontainers.role="rgw"

FROM common AS mds
RUN apt-get update && apt-get install -y --no-install-recommends "ceph-mds=${CEPH_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL io.ceph-testcontainers.role="mds"

FROM common AS all
RUN apt-get update && apt-get install -y --no-install-recommends "ceph-mon=${CEPH_PACKAGE_VERSION}" \
        "ceph-mgr=${CEPH_PACKAGE_VERSION}" "rbd-mirror=${CEPH_PACKAGE_VERSION}" \
        "cephfs-mirror=${CEPH_PACKAGE_VERSION}" "ceph-osd=${CEPH_PACKAGE_VERSION}" \
        "radosgw=${CEPH_PACKAGE_VERSION}" "ceph-mds=${CEPH_PACKAGE_VERSION}" \
    && rm -rf /var/lib/apt/lists/* && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
LABEL io.ceph-testcontainers.role="all"
