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
Ceph testcontainers Debian image: licenses and corresponding source

This image is built from unmodified Debian packages listed in
source-packages.txt in this directory, with their source package and version.
Each package keeps its own license; its copyright file is kept under
/usr/share/doc/<package>/copyright.

Corresponding source for a package is its Debian source package:
- Ceph packages: https://download.ceph.com/debian-${CEPH_RELEASE}/ (${CEPH_SUITE}, deb-src)
  and https://github.com/ceph/ceph
- Debian packages: https://deb.debian.org/debian/ and https://snapshot.debian.org/
  (apt-get source <source-package>=<source-version>)

Built with https://github.com/JSYoo5B/ceph-testcontainers-images (image/debian).
NOTICE
