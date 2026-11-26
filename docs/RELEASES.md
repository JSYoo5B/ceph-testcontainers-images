# Releases

[The release catalogue](../image/releases.json) defines the inputs for
building each release and records which releases and roles are published.
Being catalogued, being marked published and having a pullable registry tag
are separate facts: `releases.py list` gives the first, `releases.py published`
the second, and only the registry manifest the third. A successful promotion publishes its selected release
under these tags:

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:<variant>-<release>-<role>
ghcr.io/jsyoo5b/ceph-testcontainers-images:<variant>-<release>-<role>-linux-<architecture>
```

Variants are `official`, `debian` and `ubuntu`; roles are `essential`,
`client`, `cephfs`, `rbd-aux`, `rgw` and `all`, and the `official` variant
also publishes the optional `mgr-modules` role
([Roles](ROLES.md#the-mgr-modules-role)). A published release that turns
out to be broken is rebuilt and its tags are replaced by a later publication.
Each promotion receipt records the manifests the tags pointed to before.
Consumers select these images by tag; see
[Selecting images by tag](../README.md#selecting-images-by-tag) for what a
republication under an existing tag may and may not change.

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

For a published release it also lists, under `published`, the roles each
variant has published. The pinned inputs make a build reproducible; they are
not a way to select a published image, and the catalogue records no digest of
a published image.

`python3 image/releases.py` prints what other tools need: `list` (every
catalogued release, default first), `published` (the releases that the
catalogue marks published, default first),
`official <release> [<architecture>]` (the index or its pinned native child),
`build-args <release> <distribution>`, `role <release> <variant> <role>` (the
tag of a published role) and `plan --releases all|<list>` (the native matrix
of available cohorts).

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

The 19.2.6 release has a known RGW multisite defect: its `radosgw-admin` signs
`realm pull` requests that the same release's RGW rejects
(`x-amz-content-sha256` outside `SignedHeaders`), so RGW multisite cannot
start. The `rgw-multisite` functional scenario is therefore expected to fail for
19.2.6. That result is recorded as auxiliary evidence and does not block the
publication of its cohorts.

The influx MGR module cannot run in the official image of any catalogued
release. It imports the InfluxDB 1.x Python client (`influxdb`) as optional,
and its `can_run()` refuses with `influxdb python module not found`. Ceph's
packages list `python3-influxdb` only as a weak dependency (`ceph-mgr` on
Squid and Tentacle, `ceph-mgr-influx` on Umbrella), and the official images
do not install it. EPEL 9 packages it for the CentOS Stream 9 images of Squid
and Tentacle; none of the repositories configured in the Rocky Linux 10 image
of Umbrella provides it. On
Squid 19.2.5, Tentacle 20.2.4 and Umbrella 21.2.0 a running MGR reports influx,
and only influx, with `can_run` false, from the official image and from the
mgr-modules role alike. The role reproduces the official image, so influx is
recorded as a module that cannot run in the source and its check does not
require it ([Roles](ROLES.md#project-bundle)).

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
`publish`. It plans the native matrix of the available cohorts and runs
`image/ci.py --scope full` for each one. The build and the image requirement
(quick) check of every role on the cohort's platform (`quick/check-report.json`)
are the publication gate. The functional scenarios then run as an auxiliary
indicator: the split/aggregate topologies (`full/`) and RGW multisite without a
client image (`rgw-only/`). Their exit status, report digests and per-topology
and per-scenario statuses are written to `functional-summary.json`. A failing
or incomplete functional result does not fail the job and never blocks staging
or promotion; a published version later found broken is rebuilt and replaced.
With `publish`, each cohort stages the images that passed the requirement
check and records the functional outcome in its candidate. The promote job
then promotes and health-checks every selected release on its own, and its
receipt carries each cohort's functional outcome:

- a release is promoted only when every available cohort passed the
  requirement check and was staged in the same run and revision, and a cohort
  the catalogue marks unavailable is neither required nor accepted;
- a release with a failed cohort stays unpublished without blocking the
  promotion of the others, and the job fails when any selected release is
  incomplete.

Reviewers can run the same checks on a pull request with
`/ci run image-quick [on <release>|all]` (official `amd64`) or
`/ci run image-full [on <release>|all]` (every available cohort). A full
check's job result likewise reflects the build and the requirement check; the
functional outcome is in the uploaded `functional-summary.json` and a warning
annotation.

The first promotion of a release is followed by a change that adds its
`published` entry to the catalogue, with the roles that the promotion
published for each variant. A later publication of the same tags needs no
change. The extension checks target the published releases and select their
role images by tag, and the weekly
[published image health workflow](../.github/workflows/published-health.yml)
checks that every published role tag still resolves and lists exactly its
available platforms, without comparing it with any recorded digest. A release
published before its variant gained mgr-modules lists no mgr-modules role
until a publication adds it, and the health check covers the roles listed.
