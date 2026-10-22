# ceph-testcontainers-images

This repository defines the requirements for Ceph images used by Ceph testcontainers modules, and provides a checker that verifies whether a given image meets them. It also extracts lighter role images from the official image.

## Purpose

Ceph testcontainers modules use the official Ceph image by default:

```text
quay.io/ceph/ceph:v20.2.4@sha256:6bb1c8a42fbc0bf87938946990b65174466997bc11c31eb5a323225a779fd8f9
```

This repository checks and publishes images for each Ceph release listed in [image/releases.py](image/releases.py):

| Release | Official image | Debian | Ubuntu |
| --- | --- | --- | --- |
| 20.2.4 (Tentacle, default) | `quay.io/ceph/ceph:v20.2.4` | bookworm | 24.04 (noble) |
| 19.2.5 (Squid) | `quay.io/ceph/ceph:v19.2.5` | bookworm | 22.04 (jammy) |

That file pins every release-specific input: the official image digest, the distribution base image digests, the download.ceph.com suite and the exact package version. download.ceph.com has no Noble build of Squid, so its Ubuntu images use Jammy. Squid's MGR `volumes` module imports `distutils.util`, which Bookworm and Jammy ship as `python3-distutils`; the Squid essential and `all` images retain it. Squid 19.2.6 is not listed: its `radosgw-admin` signs `realm pull` requests that the same release's RGW rejects, so RGW multisite cannot start.

Two needs can call for a different image:

- Size: the official image ships components that tests never use, and every container pulls all of them.
- Custom builds: tests may need to run against a patched or internally built Ceph instead of the official release.

With size in mind, the requirements are split by dependency: `essential`, `client`, `cephfs`, `rbd-aux` and `rgw`, so each container pulls only what it runs. An image that holds every component (`all`) is also allowed, and it can still be much lighter than the official image as long as it meets the requirements.

The requirements keep the components that tests depend on and leave out those that only operations need, such as the dashboard. How a custom image is built is up to its owner; this repository defines the requirements and checks images against them. For the size need alone, role images can be extracted directly from the official image.

Cluster setup always belongs to the testcontainers module, which supplies configuration, keyrings, bootstrap commands and entrypoints at run time. Images carry no setup logic, which is why the official image works unmodified.

## Layout

```text
docs/IMAGE_REQUIREMENTS.md   Runtime requirements by role
docs/TEST_SCENARIOS.md       Checker usage and test pass criteria
docs/ROLE_IMAGES.md          Role images extracted from the official image
docs/EXTENSIONS.md           Opt-in extension images and their contracts
image/check.py               Checker for any local image
image/check-runtime.sh       Quick check probe, run inside each image
image/extension_check.py     Checker for opt-in extension images
image/extensions/            Extension images and their probes
image/publish.py             Publish CI-tested images and promote release tags
image/releases.py            Checked releases and their pinned inputs
image/functional/            Functional scenarios used by the full check
image/roles/                 Role image extraction from the official image
image/debian/, image/ubuntu/  Role images from distribution packages
```

Unit tests live in `tests/` next to each tool. Reports and logs go to the ignored `artifacts/` directory.

## Versioned dependency roles

The default production [roles-v2 contract](docs/ROLES_V2.md) uses essential, client,
cephfs, rbd-aux, rgw and all. It separates ordinary client execution from
mirror/cache and RGW administration. Existing tags and the legacy checker
contract retain their meanings. Build with `make roles-v2-images`; select
`--contract roles-v2` in the checker and publisher. V2 publication uses a
separate `roles-v2-<variant>-<release>-<role>` namespace.

## Legacy role diagnostics

| Role | Contents |
| --- | --- |
| `control` | MON, MGR, CLI and Python clients, cryptsetup, encrypted RBD and striper clients, RBD and CephFS mirror daemons, the RBD immutable object cache daemon, `pgrep` |
| `osd` | OSD, BlueStore, object classes including hello/lock and plugins |
| `rgw` | RGW and its admin CLI |
| `mds` | CephFS MDS |
| `all` | Every component of the four roles in one image |

See [Image Requirements](docs/IMAGE_REQUIREMENTS.md) for the required runtime components and [Test Scenarios](docs/TEST_SCENARIOS.md) for validation.

## Checking an image

Requires Python 3.9 or later and a Docker engine with API 1.49 or later. The checker never builds or pulls images; prepare them locally first.

| Level | Verifies |
| --- | --- |
| Quick (default) | Required executables, libraries and classes exist and load |
| Full (`--full`) | The quick check, then every functional scenario on real clusters, including multi-cluster replication |

```sh
python3 image/check.py --image all=my-company/ceph:dev
python3 image/check.py --image all=my-company/ceph:dev --full

python3 image/check.py \
  --image control=my-company/ceph-control:dev \
  --image osd=my-company/ceph-osd:dev \
  --image rgw=my-company/ceph-rgw:dev \
  --image mds=my-company/ceph-mds:dev \
  --full
```

`make official-check` runs the quick and full check against the official image, which must always pass.

## Role images from the official image

```sh
make role-images        # extract, smoke test and quick check
make role-images-full   # extract, smoke test and full check
```

The output is `ceph-testcontainers:official-<release>-<role>` for the four roles and `all`. See [Role Images](docs/ROLE_IMAGES.md).

## Images from distribution packages

[image/debian](image/debian/Dockerfile) (bookworm-slim) and [image/ubuntu](image/ubuntu/Dockerfile) build the same five roles by installing only the needed packages from Ceph's own Debian and Ubuntu repositories at download.ceph.com. They are built with `make debian-images` and `make ubuntu-images`, which take `CEPH_RELEASE` (default `20.2.4`), and tagged `<distribution>-<release>-<role>`. The arm64 Ubuntu images set `TCMALLOC_STACKTRACE_METHOD=generic_fp`, because Ceph's Noble daemons crash with SIGILL in the default tcmalloc stack unwinder on some ARM64 hosts; on amd64 the same setting makes them crash, so it is not set there. Like the extracted images, each one records its packages, licenses and source pointers under `/usr/share/ceph-testcontainers/`.

### Experimental Alpine stable Squid

`image/alpine/` builds the five roles from Alpine **3.24.2** and its stable
`v3.24/main` and `v3.24/community` repositories. The base digest and Ceph APK
version **19.2.4-r1** are pinned in its Dockerfile. No edge or testing repository
is used. This is a separate **19.2.4** experiment: it is not part of
`image/releases.py`, the publication workflow or the published **19.2.5** tags.
`CEPH_RELEASE` continues to select the existing published image variants.

```sh
make alpine-images PLATFORM=linux/amd64
python3 image/check.py --platform linux/amd64 --full \
  --image control=ceph-testcontainers:alpine-19.2.4-control \
  --image osd=ceph-testcontainers:alpine-19.2.4-osd \
  --image rgw=ceph-testcontainers:alpine-19.2.4-rgw \
  --image mds=ceph-testcontainers:alpine-19.2.4-mds \
  --image all=ceph-testcontainers:alpine-19.2.4-all
```

Use `PLATFORM=linux/arm64` to build on a native ARM64 Docker host. Build success
alone does not establish runtime compatibility; run the checker on that host.
The target supports the same opt-in `BUILD_CACHE_REF` and `BUILD_CACHE_WRITE`
settings as the distribution builds above.

The control and `all` images set
`CEPH_ARGS=--mgr-subinterpreter-modules=*` to restore Squid's per-module Python
interpreters. Alpine's backported shared-interpreter default can otherwise
dispatch a CephFS mirroring command to the wrong MGR module.
They request an 8 MiB worker stack for `cephfs-mirror` through its ELF
`PT_GNU_STACK` header: its directory replay allocates more stack than musl's
default. Only the stack-size field changes; executable code and stack permission
flags are preserved. `modifications.json` records the original and modified
hashes and sizes alongside the source manifest.
The OSD image installs the daemon and base packages without the OSD tools and
ceph-volume metapackage. The control image needs the parent `ceph19` package
for the immutable object cache and parent-cache plugin, which also brings in
the other daemons; `all` consequently reuses it. Installed package versions,
SPDX license expressions, source origins and aports commits are recorded under
`/usr/share/ceph-testcontainers/`, with Ceph's upstream `COPYING` notice under
`/usr/share/licenses/ceph/`.

## Extension images

Some module features need tools that the official image does not ship, such as `tc` for kernel packet delay. They are opt-in: the caller selects an extension image for that feature, and baseline features never need one. [Extension Images](docs/EXTENSIONS.md) defines each extension's contract and how to check an image against it.

```sh
make extension-images   # build the netem helper
make extension-check    # check it, including a delay inside an official-image container
```

The netem helper is published as `ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-netem-v1` for `linux/amd64` and `linux/arm64`. It is checked and published apart from the release images below, and neither blocks the other.

## Published images

Production images use
`ghcr.io/jsyoo5b/ceph-testcontainers-images:roles-v2-<variant>-<release>-<role>`
for `linux/amd64` and `linux/arm64`. Roles are `essential`, `client`,
`cephfs`, `rbd-aux`, `rgw` and the aggregate `all`. Select only the roles
required by the feature, as documented in [Dependency roles](docs/ROLES_V2.md).

The [roles-v2 workflow](.github/workflows/roles-v2.yml) is the production
entry point: PRs and main pushes build and quick-check both supported
releases, all three variants and both native architectures. A manual run
selects one release, independently runs the complete split and aggregate
functional suites and the RGW-without-client proof, then optionally stages
and promotes the exact tested image identities with `publish=true`.
Promotion requires every variant/architecture candidate; failed or skipped
checks block it. Registry health is verified after promotion. Consumer-side
registry pulls and native interoperability remain separate evidence.

Each release tag is an index of two platform images, also retained under
`-linux-amd64` and `-linux-arm64` tags. The weekly
[published image health workflow](.github/workflows/published-health.yml)
checks the dependency-role namespace and the extension helper tags. Keep
both platform images reachable when cleaning registry storage.

The [checker workflow](.github/workflows/test.yml) runs unit and extension
checks by default. Its former control/osd/mds/rgw campaign is available only
through manual `legacy_images=true` for historical comparisons. These tags
are not rebuilt or republished by ordinary PR/main CI. Existing diagnostic
checker/build commands retain their explicit legacy contract. Netem's
functional and post-publication tests now target the essential image that
hosts OSDs, alongside the unmodified official source image.

| Variant | Source |
| --- | --- |
| `official` | Runtime extracted from the pinned official image |
| `debian` | Debian bookworm-slim with pinned Ceph packages |
| `ubuntu` | Ubuntu noble (Tentacle) or jammy (Squid) with pinned Ceph packages |

The Alpine 19.2.4 experiment remains separate from the supported production
release/variant publication matrix. See [Alpine](#experimental-alpine-stable-squid).

## Development

`make check` runs the host unit tests of the checkers, the publisher, the functional harness and the role extraction. The release jobs depend only on `make base-check`; the extension jobs run `make extension-test`, so that neither blocks the other. [The production workflow](.github/workflows/roles-v2.yml) checks official-derived, Debian and Ubuntu dependency roles on native AMD64 and ARM64. Full publication checks cover split roles and `all` independently.

## License

The code and documents in this repository are licensed under the [MIT License](LICENSE).

The checkers and the functional scenarios run Ceph only inside containers and include no Ceph code. The netem helper contains no Ceph component; its packages keep their licenses as described in [Extension Images](docs/EXTENSIONS.md). Role images built with this repository are different: they contain Ceph and distribution packages, each under its own license (Ceph is mostly LGPL-2.1 or LGPL-3, see its [COPYING](https://github.com/ceph/ceph/blob/v20.2.4/COPYING)). Images built by `image/roles/`, `image/debian/` and `image/ubuntu/` keep their license files and carry `/usr/share/ceph-testcontainers/SOURCES.txt` with `source-packages.txt`, which name the source package of every installed package and where it is published. Whoever distributes such images must meet those licenses, including making the corresponding source available where required.
