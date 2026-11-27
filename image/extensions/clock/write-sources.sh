#!/bin/sh
# Write the license and corresponding-source notice of the clock helper. The
# published image holds no Debian package: the build stage's packages only
# compile the interposer.
set -eu
: "${OUTPUT:?}" "${BUILD_BASE:?}"
directory=$OUTPUT/usr/share/ceph-testcontainers
mkdir -p "$directory"
cat > "$directory/SOURCES.txt" <<NOTICE
Ceph testcontainers clock helper: licenses and corresponding source

This image contains no Ceph component, no package and no executable. It holds
one file to be copied elsewhere: the libfaketime interposer at
/opt/ceph-testcontainers/clock/libfaketime.so.1, and its build manifest at
/opt/ceph-testcontainers/clock/build.json.

libfaketime is built unmodified from the upstream commit recorded in the
manifest, with the upstream default compile flags (no INTERCEPT_SYSCALL). Its
license (GPL-2.0) is kept at /usr/share/doc/libfaketime/copyright and the source
is https://github.com/wolfcw/libfaketime at that commit.

A build stage compiled it on the pinned $BUILD_BASE base with Debian's
build-essential; the compiler and C library versions are recorded in the
manifest. None of the build stage's packages is part of this image.

Built with https://github.com/JSYoo5B/ceph-testcontainers-images (image/extensions/clock).
NOTICE
