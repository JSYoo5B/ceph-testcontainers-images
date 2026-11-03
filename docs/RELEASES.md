# Releases

Every Ceph release this repository builds, checks and publishes is listed in
[the release catalogue](../image/releases.json), which
[image/releases.py](../image/releases.py) loads and validates. Each catalogued
release is built and checked the same way and published under the same names:

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:<variant>-<release>-<role>
ghcr.io/jsyoo5b/ceph-testcontainers-images:<variant>-<release>-<role>-linux-<architecture>
```

Variants are `official`, `debian` and `ubuntu`; roles are `essential`,
`client`, `cephfs`, `rbd-aux`, `rgw` and `all`. A published release that turns
out to be broken is rebuilt and its tags are replaced by a later publication.
Each promotion receipt records the manifests the tags pointed to before.

## Catalogue

The catalogue holds 14 releases:

- Squid: 19.2.0 through 19.2.7.
- Tentacle: 20.2.0 through 20.2.4. 20.2.4 is the default.
- Umbrella: 21.2.0.

For every release it pins:

- the official `quay.io/ceph/ceph` index digest and the digest of each native
  child (`amd64`, `arm64`);
- for Debian and Ubuntu, the base image digest, the download.ceph.com suite,
  the exact Ceph package version and the extra manager packages;
- the signed APT metadata that establishes those packages: the `Release` file,
  the per-architecture `Packages` indexes and, for extra Ceph packages, their
  archive identities;
- which architectures of each variant have usable inputs and, for every other
  architecture, the reason (and, where relevant, the evidence) why not.

`python3 image/releases.py` prints what other tools need: `list` (every
catalogued release, default first), `published` (the releases with role
indexes in [the digest inventory](../.github/ceph-role-images.json)),
`official <release> [<architecture>]` (the index or its pinned native child),
`build-args <release> <distribution>`, `role <release> <variant> <role>` (an
immutable published role reference) and `plan --releases all|<list>` (the
native matrix of available cohorts).

## Distribution inputs

Debian images use Bookworm. The Debian packages of 19.2.0 to 19.2.3 carry the
`-1~bpo12+1` version suffix; later releases use `-1bookworm`.

download.ceph.com has no Noble build of Squid, so Squid's Ubuntu images use
Jammy. Tentacle 20.2.0 and 20.2.3 also use Jammy because their Noble
repositories are absent; 20.2.1, 20.2.2 and 20.2.4 use Noble. Umbrella uses
Noble.

Squid's MGR `volumes` module imports `distutils.util`, which Bookworm and Jammy
ship separately as `python3-distutils`. Squid's distribution inputs install it,
and the Squid essential and `all` images retain it. Tentacle and Umbrella do
not import it.

Umbrella moved manager modules out of `ceph-mgr-modules-core`. Of the four
modules that essential requires, `volumes` and `rbd_support` stay in
modules-core, `mirroring` is in `ceph-mgr-mirroring` and `snap_schedule` is in
`ceph-mgr-snap-schedule`, in the official RPM image and in the Bookworm and
Noble repositories alike. The distribution inputs install both split packages
at the matching Ceph version, and every installed split module package is
selected into essential alongside the core manager modules, for the official
input too. Umbrella also separates common OSD files from the classic
implementation: extraction keeps the installed `ceph-osd` alternatives link
chain and its `ceph-osd-classic` target in essential, so the public entrypoint
is unchanged.

Some official images omit Ceph's `COPYING` although their RPM metadata names
it. For the builds listed in
[image/roles/ceph-licenses.json](../image/roles/ceph-licenses.json), official
role extraction restores only the vendored upstream text pinned to the exact
observed Ceph build commit and SHA256. Existing license files are preserved,
every affected role records the addition in `license-supplement.json`, no Ceph
executable is changed and the assembly stays offline.

## Unavailable cohorts

Of the 84 release/variant/architecture cohorts, 79 have usable inputs. The
other five are marked unavailable in the catalogue with their reason. They are
never built or substituted with another distribution or release, and promotion
publishes those releases without that child: the variant's role indexes list
only the available platform.

- Debian 19.2.0 to 19.2.3 on `arm64`: the matching-release Ceph packages are
  absent, and the alternative Debian suites return 404.
- Ubuntu 19.2.6 on `arm64`: the signed Jammy metadata requires a 437982-byte
  `libradosstriper1` archive with SHA256 `f9503cf1…`, but canonical and
  cache-bypassed downloads both return 437970 bytes with SHA256 `78d35ff9…`.
  The `Release` signature and the metadata hashes verify; the package bytes do
  not, and Noble has no repository for this release. The catalogue keeps the
  full expected and observed identities. No integrity check is disabled to use
  the mismatching archive.

## Host requirements

Umbrella's official `amd64` image is built on Rocky Linux 10, whose executables
require an x86-64-v3 CPU. The catalogue records this as the cohort's
`cpu_baseline`. Before building such a cohort, [image/ci.py](../image/ci.py)
reads the CPU levels from the glibc loader. A host without the level is
reported as `environment_ineligible` (exit status 3, recorded in
`preflight.json`), which is distinct from a build or check failure.

## Known release defects

In 19.2.6, `radosgw-admin` signs `realm pull` requests that the same release's
RGW rejects (`x-amz-content-sha256` outside `SignedHeaders`), so RGW multisite
cannot start. The full check's `rgw-multisite` scenario fails for 19.2.6, and
the publication gate does not publish its cohorts.

## Building, checking and publishing

`make role-images VARIANT=<variant> CEPH_RELEASE=<release> PLATFORM=linux/<architecture>`
builds the roles of one cohort. The official variant extracts the catalogue's
pinned native child (`OFFICIAL_SOURCE`); the distribution variants build their
source runtime with the catalogue's build arguments. `python3 image/ci.py
--release <release> --variant <variant> --architecture <architecture> --scope
quick|full --output <directory>` runs the cohort preflight, the build and the
requested checks.

The manual [images workflow](../.github/workflows/images.yml) takes `releases`
(`all` or comma-separated catalogued versions, default `20.2.4`) and
`publish`. It plans the native matrix of the available cohorts, builds each one
and runs the full check, including the split/aggregate topologies and RGW
multisite without a client image. With `publish`, each cohort stages its
exactly tested images. The promote job then promotes and health-checks every
selected release on its own:

- a release is promoted only when every available cohort passed in the same
  run and revision, and a cohort the catalogue marks unavailable is neither
  required nor accepted;
- a release with a failed cohort stays unpublished without blocking the
  promotion of the others, and the job fails when any selected release is
  incomplete.

Reviewers can run the same checks on a pull request with
`/ci run image-quick [on <release>|all]` (official `amd64`) or
`/ci run image-full [on <release>|all]` (every available cohort).

The index digests of published releases are recorded in
[the digest inventory](../.github/ceph-role-images.json). The extension checks
target those releases, and the weekly
[published image health workflow](../.github/workflows/published-health.yml)
checks that every recorded index still lists exactly its available platforms.
