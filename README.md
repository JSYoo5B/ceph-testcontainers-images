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

That file pins every release-specific input: the official image digest, the distribution base image digests, the download.ceph.com suite and the exact package version. download.ceph.com has no Noble build of Squid, so its Ubuntu images use Jammy. Squid's MGR `volumes` module imports `distutils.util`, which Bookworm and Jammy ship as `python3-distutils`; the Squid control and `all` images install it. Squid 19.2.6 is not listed: its `radosgw-admin` signs `realm pull` requests that the same release's RGW rejects, so RGW multisite cannot start.

Two needs can call for a different image:

- Size: the official image ships components that tests never use, and every container pulls all of them.
- Custom builds: tests may need to run against a patched or internally built Ceph instead of the official release.

With size in mind, the requirements are split by role: `control`, `osd`, `rgw` and `mds`, so each container pulls only what it runs. An image that holds every component (`all`) is also allowed, and it can still be much lighter than the official image as long as it meets the requirements.

The requirements keep the components that tests depend on and leave out those that only operations need, such as the dashboard. How a custom image is built is up to its owner; this repository defines the requirements and checks images against them. For the size need alone, role images can be extracted directly from the official image.

Cluster setup always belongs to the testcontainers module, which supplies configuration, keyrings, bootstrap commands and entrypoints at run time. Images carry no setup logic, which is why the official image works unmodified.

## Layout

```text
docs/IMAGE_REQUIREMENTS.md   Runtime requirements by role
docs/TEST_SCENARIOS.md       Checker usage and test pass criteria
docs/ROLE_IMAGES.md          Role images extracted from the official image
image/check.py               Checker for any local image
image/check-runtime.sh       Quick check probe, run inside each image
image/publish.py             Publish CI-tested images and promote release tags
image/releases.py            Checked releases and their pinned inputs
image/functional/            Functional scenarios used by the full check
image/roles/                 Role image extraction from the official image
image/debian/, image/ubuntu/  Role images from distribution packages
```

Unit tests live in `tests/` next to each tool. Reports and logs go to the ignored `artifacts/` directory.

## Roles

| Role | Contents |
| --- | --- |
| `control` | MON, MGR, CLI and Python clients, cryptsetup, encrypted RBD and striper clients, RBD and CephFS mirror daemons |
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

## Published images

Images are published as `ghcr.io/jsyoo5b/ceph-testcontainers-images:<variant>-<release>-<role>` for `linux/amd64` and `linux/arm64`, with `<release>` one of the releases above and `<role>` one of `control`, `osd`, `rgw`, `mds` and `all`.

Pull requests and pushes to `main` run the unit tests and the quick requirement checks of every release, variant and platform; documentation-only changes skip them. The full functional suite runs only in a manual run of the [checker workflow](.github/workflows/test.yml), whose `release` input selects one release or `all`.

To publish, run that workflow with `publish` enabled. Publishing one release leaves the other releases' tags untouched, so a fix for one release never waits for another. For an existing GHCR package, grant this repository **Write** access under **Manage Actions access** in the package settings; the workflow's `packages: write` permission also needs that [package access](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

The workflow builds all five roles for each variant on native AMD64 and ARM64 runners and runs quick checks and the full functional suite for both the four-role combination and `all`. Each passing job uploads the exact tested image IDs under CI candidate tags, without rebuilding. Every release runs the same jobs. Only after all build/check jobs and unmodified Quay comparisons of every selected release pass does the promotion job overwrite, for each of those releases, the 30 platform tags and 15 two-platform release tags with those digests. Failed or skipped checks block promotion.

The release then calls the [published images workflow](.github/workflows/published-images.yml) to pull those immutable platform digests from GHCR and run the full suite again. To check an existing publication separately, supply its digest map as `release -> variant -> architecture -> role -> sha256 digest` through that workflow's `images` input. Reports record the tested local image IDs, registry digests, Ceph versions, platforms and cleanup results; promotion also records the previous release-tag digests.

Each release tag is an index of two platform images that also carry their own `-linux-amd64` and `-linux-arm64` tags. Deleting those platform tags, or their untagged versions, leaves a release tag that resolves but cannot be pulled. The weekly [published image health workflow](.github/workflows/published-health.yml) runs `python3 image/publish.py health` to catch that; run it after cleaning up GHCR package versions.

| Variant | Source |
| --- | --- |
| `official` | Extracted from the official image with `image/roles/` |
| `debian` | Debian bookworm-slim with Ceph's Debian packages (`image/debian/`) |
| `ubuntu` | Ubuntu 24.04 with Ceph's Ubuntu packages (`image/ubuntu/`) |

The `-linux-amd64` and `-linux-arm64` tags identify the single-platform
images referenced by the multi-platform tags. Check the image ID and
platform you intend to use with the [checker](docs/TEST_SCENARIOS.md).
A successful report establishes compatibility for its recorded scenarios.

## Development

`make check` runs the host unit tests of the checker, the functional harness and the role extraction. [The workflow](.github/workflows/test.yml) checks Debian, Ubuntu and the unmodified official image on native AMD64 and ARM64 runners. Distribution builds check both mixed roles and `all`.

## License

The code and documents in this repository are licensed under the [MIT License](LICENSE).

The checker and the functional scenarios run Ceph only inside containers and include no Ceph code. Role images built with this repository are different: they contain Ceph and distribution packages, each under its own license (Ceph is mostly LGPL-2.1 or LGPL-3, see its [COPYING](https://github.com/ceph/ceph/blob/v20.2.4/COPYING)). Images built by `image/roles/`, `image/debian/` and `image/ubuntu/` keep their license files and carry `/usr/share/ceph-testcontainers/SOURCES.txt` with `source-packages.txt`, which name the source package of every installed package and where it is published. Whoever distributes such images must meet those licenses, including making the corresponding source available where required.
