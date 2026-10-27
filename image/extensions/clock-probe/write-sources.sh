#!/bin/sh
# Record installed Debian packages with their source package and version, so the
# image carries its own licenses and corresponding-source pointers.
set -eu
directory=/usr/share/ceph-testcontainers
mkdir -p "$directory"
{
    printf '# package\tversion\tsource-package\tsource-version\n'
    dpkg-query -W -f='${Package}\t${Version}\t${source:Package}\t${source:Version}\n' | sort
} > "$directory/source-packages.txt"
cat > "$directory/SOURCES.txt" <<NOTICE
Ceph testcontainers clock-probe helper: licenses and corresponding source

This image contains no Ceph component. Its Debian packages are unmodified and
listed in source-packages.txt in this directory, with their source package and
version. Each package keeps its own license; its copyright file is kept under
/usr/share/doc/<package>/copyright.

Corresponding source for a Debian package is its Debian source package:
https://deb.debian.org/debian/ and https://snapshot.debian.org/
(apt-get source <source-package>=<source-version>)

libfaketime is not a Debian package here. It is built unmodified from the
upstream commit recorded in /opt/ceph-testcontainers/libfaketime/build.json
(clock-probe-build-info prints it), twice, without and with -DINTERCEPT_SYSCALL.
Its license (GPL-2.0) is kept at /usr/share/doc/libfaketime/copyright and the
source is https://github.com/wolfcw/libfaketime at that commit.

Built with https://github.com/JSYoo5B/ceph-testcontainers-images (image/extensions/clock-probe).
NOTICE
