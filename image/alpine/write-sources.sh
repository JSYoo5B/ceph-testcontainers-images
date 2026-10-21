#!/bin/sh
# Keep Alpine's installed-package identities, SPDX expressions and exact aports
# recipe commits. The Dockerfile records any ELF stack-header adjustment.
set -eu
directory=${SOURCE_NOTICE_DIR:-/usr/share/ceph-testcontainers}
database=${APK_INSTALLED_DB:-/lib/apk/db/installed}
mkdir -p "$directory"
{
    printf '# package\tversion\tsource-package\tlicense\taports-commit\tproject-url\n'
    awk '
        function emit() {
            if (name != "") printf "%s\t%s\t%s\t%s\t%s\t%s\n", name, version, origin, license, commit, url
            name=version=origin=license=commit=url=""
        }
        /^P:/ {name=substr($0,3)}
        /^V:/ {version=substr($0,3)}
        /^o:/ {origin=substr($0,3)}
        /^L:/ {license=substr($0,3)}
        /^c:/ {commit=substr($0,3)}
        /^U:/ {url=substr($0,3)}
        /^$/ {emit()}
        END {emit()}
    ' "$database" | sort
} > "$directory/source-packages.txt"
cat > "$directory/SOURCES.txt" <<NOTICE
Ceph testcontainers Alpine image: licenses and corresponding source

This image contains Alpine ${ALPINE_BRANCH} packages listed in
source-packages.txt. Each row records its version, source package (origin),
SPDX license expression, aports commit and upstream project URL. Package
notices supplied by those packages are retained.
Ceph's upstream license notice is /usr/share/licenses/ceph/COPYING.
In control/all, modifications.json records the cephfs-mirror ELF GNU_STACK
size adjustment, with original and modified hashes. Executable code and
stack permission flags are unchanged; image/alpine/Dockerfile contains the
complete adjustment procedure.

Corresponding source, patches and build instructions are identified by each
package's aports commit and origin:
- https://gitlab.alpinelinux.org/alpine/aports/-/tree/<aports-commit>
  Find main/<source-package>/APKBUILD or community/<source-package>/APKBUILD
  at that commit. Its source entries identify the original source archives
  and patches; subpackages use the same origin recipe.
- Alpine source archives: https://distfiles.alpinelinux.org/distfiles/
- Ceph ${CEPH_RELEASE}: https://github.com/ceph/ceph/tree/v${CEPH_RELEASE}

Built with https://github.com/JSYoo5B/ceph-testcontainers-images (image/alpine).
NOTICE
