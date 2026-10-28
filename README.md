# ceph-testcontainers-images

This repository defines the requirements for Ceph images used by Ceph testcontainers modules, and provides a checker that verifies whether a given image meets them. It also extracts lighter role images from the official image.

## Purpose

Ceph testcontainers modules use the pruned role runtime by default. Published
role indexes are pinned in [.github/ceph-role-images.json](.github/ceph-role-images.json).
The upstream official image remains an unmodified compatibility reference.

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
image/capture_experiment*.py Unwired capture file-size-limit experiment supervisor
image/extensions/            Extension images and their probes
image/publish.py             Publish CI-tested images and promote release tags
image/releases.py            Checked releases and their pinned inputs
image/functional/            Functional scenarios used by the full check
image/roles/                 Role image extraction from the official image
image/debian/, image/ubuntu/  Role images from distribution packages
```

Unit tests live in `tests/` next to each tool. Reports and logs go to the ignored `artifacts/` directory.

## Role images

The [role requirements](docs/ROLES.md) define essential, client, cephfs,
rbd-aux, rgw and all. Essential contains MON/MGR/OSD and their daemon-local
modules/plugins. Client supplies ordinary CLI and Python bindings; optional
processes and RGW administration stay with their feature role.

Build with `make role-images VARIANT=official` (or debian, ubuntu, alpine).
`image/check.py` and `image/publish.py` use this contract directly.

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
  --image essential=my-company/ceph-essential:dev \
  --image client=my-company/ceph-client:dev \
  --image cephfs=my-company/ceph-cephfs:dev \
  --image rbd-aux=my-company/ceph-rbd-aux:dev \
  --image rgw=my-company/ceph-rgw:dev \
  --full
```

`make official-check` runs the quick and full check against the official image, which must always pass.

## Role images from the official image

```sh
make role-images        # extract, smoke test and quick check
make role-images-full   # extract, smoke test and full check
```

The output is `ceph-testcontainers:official-<release>-<role>` for the five roles and `all`. See [Role Images](docs/ROLE_IMAGES.md).

## Images from distribution packages

[image/debian](image/debian/Dockerfile) (bookworm-slim) and [image/ubuntu](image/ubuntu/Dockerfile) provide source runtimes for the same five roles and aggregate by installing only the needed packages from Ceph's own Debian and Ubuntu repositories at download.ceph.com. They are built with `make debian-images` and `make ubuntu-images`, which take `CEPH_RELEASE` (default `20.2.4`), and tagged `<distribution>-<release>-<role>`. The arm64 Ubuntu images set `TCMALLOC_STACKTRACE_METHOD=generic_fp`, because Ceph's Noble daemons crash with SIGILL in the default tcmalloc stack unwinder on some ARM64 hosts; on amd64 the same setting makes them crash, so it is not set there. Like the extracted images, each one records its packages, licenses and source pointers under `/usr/share/ceph-testcontainers/`.

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
  --image essential=ceph-testcontainers:alpine-19.2.4-essential \
  --image client=ceph-testcontainers:alpine-19.2.4-client \
  --image cephfs=ceph-testcontainers:alpine-19.2.4-cephfs \
  --image rbd-aux=ceph-testcontainers:alpine-19.2.4-rbd-aux \
  --image rgw=ceph-testcontainers:alpine-19.2.4-rgw \
  --image all=ceph-testcontainers:alpine-19.2.4-all
```

Use `PLATFORM=linux/arm64` to build on a native ARM64 Docker host. Build success
alone does not establish runtime compatibility; run the checker on that host.
The target supports the same opt-in `BUILD_CACHE_REF` and `BUILD_CACHE_WRITE`
settings as the distribution builds above.

The installed source runtime sets
`CEPH_ARGS=--mgr-subinterpreter-modules=*` to restore Squid's per-module Python
interpreters. Alpine's backported shared-interpreter default can otherwise
dispatch a CephFS mirroring command to the wrong MGR module.
The cephfs and all images retain an 8 MiB worker stack for `cephfs-mirror` through its ELF
`PT_GNU_STACK` header: its directory replay allocates more stack than musl's
default. Only the stack-size field changes; executable code and stack permission
flags are preserved. `modifications.json` records the original and modified
hashes and sizes alongside the source manifest.
The source runtime needs the parent `ceph19` package for the immutable object
cache and parent-cache plugin. The assembler then selects each role's package
closure and executable set. Installed package versions,
SPDX license expressions, source origins and aports commits are recorded under
`/usr/share/ceph-testcontainers/`, with Ceph's upstream `COPYING` notice under
`/usr/share/licenses/ceph/`.

## Extension images

Some module features need tools that the official image does not ship, such as `tc` for kernel packet delay. They are opt-in: the caller selects an extension image for that feature, and baseline features never need one. [Extension Images](docs/EXTENSIONS.md) defines each extension's contract and how to check an image against it.

```sh
make extension-images   # build the netem helper
make extension-check    # check it, including a delay inside an official-image container
```

The netem helper is published as `ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-netem-v1` for `linux/amd64` and `linux/arm64`. A publication run from the default branch publishes the packet-capture helper the same way as `ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-capture-v1`. Each helper is checked and published apart from the other and from the release images below, and none blocks another. The clock-probe helper (a compiler and two pinned libfaketime builds for clock scratch work) has only its quick check and is not published.

## Published images

Production images use
`ghcr.io/jsyoo5b/ceph-testcontainers-images:<variant>-<release>-<role>`
for `linux/amd64` and `linux/arm64`. Roles are `essential`, `client`,
`cephfs`, `rbd-aux`, `rgw` and the aggregate `all`. Select only the roles
required by the feature, as documented in [Dependency roles](docs/ROLES.md).

The [checker workflow](.github/workflows/test.yml) runs the host unit and
extension checks. It does not build a Ceph release matrix on every PR or push.
Reviewers can request additional image checks with a PR comment:

```text
/ci run image-quick
/ci run image-full on 19.2.5
/ci run image-full on all
```

Quick selects official/Tentacle/amd64 by default. Full selects all three
variants and both native architectures for the requested releases. Only an
owner, member or collaborator may run a command on an open same-repository
PR. The head SHA is frozen before checkout; native jobs have a read-only token
and cannot publish. Results appear as a commit status with the run link.

The manual [images workflow](.github/workflows/images.yml) selects one release
and runs the complete split/aggregate and RGW-without-client proofs before
optional publication. Promotion requires all six tested native cohorts;
failed or skipped checks block it. Publication and registry health remain
separate from downstream consumer validation.

The current qualified registry indexes are recorded in
[the published digest map](.github/ceph-role-images.json). Consumers and
extension checks can use these immutable references without rebuilding or
republishing an image for a name change. The weekly
[published image health workflow](.github/workflows/published-health.yml)
checks both native children of every pinned role and the extension tags.

| Variant | Source |
| --- | --- |
| `official` | Runtime extracted from the pinned official image |
| `debian` | Debian bookworm-slim with pinned Ceph packages |
| `ubuntu` | Ubuntu noble (Tentacle) or jammy (Squid) with pinned Ceph packages |

The Alpine 19.2.4 experiment remains separate from the supported production
release/variant publication matrix. See [Alpine](#experimental-alpine-stable-squid).

## Development

`make check` runs the host unit tests of the checkers, the publisher, the functional harness and the role extraction. The release jobs depend only on `make base-check`; the extension jobs run `make extension-test`, so that neither blocks the other. [The manual production workflow](.github/workflows/images.yml) checks official-derived, Debian and Ubuntu dependency roles on native AMD64 and ARM64. Full publication checks cover split roles and `all` independently.

## License

The code and documents in this repository are licensed under the [MIT License](LICENSE).

The checkers and the functional scenarios run Ceph only inside containers and include no Ceph code. The netem helper contains no Ceph component; its packages keep their licenses as described in [Extension Images](docs/EXTENSIONS.md). Role images built with this repository are different: they contain Ceph and distribution packages, each under its own license (Ceph is mostly LGPL-2.1 or LGPL-3, see its [COPYING](https://github.com/ceph/ceph/blob/v20.2.4/COPYING)). Images built by `image/roles/`, `image/debian/` and `image/ubuntu/` keep their license files and carry `/usr/share/ceph-testcontainers/SOURCES.txt` with `source-packages.txt`, which name the source package of every installed package and where it is published. Whoever distributes such images must meet those licenses, including making the corresponding source available where required.
