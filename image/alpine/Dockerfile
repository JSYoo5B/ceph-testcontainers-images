# Experimental stable Alpine Squid images. This release is separate from the
# published release matrix: Alpine 3.24 ships Ceph 19.2.4, not 19.2.5.
ARG BASE_IMAGE=alpine:3.24.2@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
ARG BASE_NAME=alpine:3.24.2

FROM ${BASE_IMAGE} AS common
ARG BASE_NAME
ENV CEPH_RELEASE=19.2.4 CEPH_PACKAGE_VERSION=19.2.4-r1 ALPINE_BRANCH=v3.24
COPY write-sources.sh /usr/local/lib/ceph-testcontainers/write-sources.sh
ADD --chmod=644 --checksum=sha256:2cca0fe8cbdab212a49817ff4d216db06629bc85d941ac3c2e6ca8b328b28d71 \
    https://raw.githubusercontent.com/ceph/ceph/v19.2.4/COPYING /usr/share/licenses/ceph/COPYING
RUN printf '%s\n' "https://dl-cdn.alpinelinux.org/alpine/${ALPINE_BRANCH}/main" \
        "https://dl-cdn.alpinelinux.org/alpine/${ALPINE_BRANCH}/community" > /etc/apk/repositories \
    && apk add --no-cache ca-certificates coreutils \
    && mkdir -p /etc/ceph /var/lib/ceph /var/run/ceph /var/log/ceph \
    && chmod 1777 /tmp
LABEL org.opencontainers.image.source="https://github.com/JSYoo5B/ceph-testcontainers-images" \
      org.opencontainers.image.base.name="${BASE_NAME}" \
      org.opencontainers.image.version="19.2.4" \
      org.opencontainers.image.description="Experimental Ceph Squid role image from Alpine stable packages" \
      io.ceph-testcontainers.source-notice="/usr/share/ceph-testcontainers/SOURCES.txt"

FROM common AS all
# The cache daemon and librbd parent-cache plugin belong to the ceph19 parent
# package, which also installs the other daemons. Do not silently omit them.
RUN apk add --no-cache "ceph19=${CEPH_PACKAGE_VERSION}" "ceph19-radosgw=${CEPH_PACKAGE_VERSION}" \
        "rbd-mirror19=${CEPH_PACKAGE_VERSION}" "cephfs19-mirror=${CEPH_PACKAGE_VERSION}" \
        "libradosstriper19=${CEPH_PACKAGE_VERSION}" cryptsetup procps py3-setuptools \
    && sh /usr/local/lib/ceph-testcontainers/write-sources.sh
# Alpine backports a shared-main-interpreter default to Squid. Its command
# registries can dispatch mirroring commands to another module. Restore Squid's
# per-module interpreters through the packaged startup option, without patches.
ENV CEPH_ARGS=--mgr-subinterpreter-modules=*
# PeerReplayer::propagate_deleted_entries allocates 512 dirent records on the
# stack, exceeding musl's default worker stack. Request the usual 8 MiB through
# PT_GNU_STACK; keep executable permissions and every other ELF byte unchanged.
RUN python3 - /usr/bin/cephfs-mirror /usr/share/ceph-testcontainers <<'PY'
import hashlib
import json
from pathlib import Path
import struct
import sys

binary, notice = map(Path, sys.argv[1:])
original = binary.read_bytes()
if original[:6] != b'\x7fELF\x02\x01':
    raise SystemExit('Expected a little-endian ELF64 cephfs-mirror')
machine = struct.unpack_from('<H', original, 18)[0]
if machine not in (62, 183):
    raise SystemExit('Expected AMD64 or ARM64 cephfs-mirror')
offset = struct.unpack_from('<Q', original, 32)[0]
stride, count = struct.unpack_from('<HH', original, 54)
if stride != 56 or offset + stride * count > len(original):
    raise SystemExit('Invalid ELF program header table')
headers = [offset + stride * i for i in range(count)
           if struct.unpack_from('<I', original, offset + stride * i)[0] == 0x6474e551]
if len(headers) != 1:
    raise SystemExit('Expected exactly one PT_GNU_STACK header')
field = headers[0] + 40
before = struct.unpack_from('<Q', original, field)[0]
after = max(before, 8 * 1024 * 1024)
modified = bytearray(original)
struct.pack_into('<Q', modified, field, after)
assert modified[:field] == original[:field] and modified[field + 8:] == original[field + 8:]
binary.write_bytes(modified)
notice.mkdir(parents=True, exist_ok=True)
(notice / 'modifications.json').write_text(json.dumps({
    'file': str(binary), 'change': 'PT_GNU_STACK p_memsz',
    'before_bytes': before, 'after_bytes': after,
    'original_sha256': hashlib.sha256(original).hexdigest(),
    'modified_sha256': hashlib.sha256(modified).hexdigest(),
    'reason': 'CephFS mirror uses a stack allocation larger than the musl default',
}, indent=2) + '\n')
PY
LABEL io.ceph-testcontainers.role="all"
