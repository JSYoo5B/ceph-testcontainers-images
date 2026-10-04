# ceph-testcontainers-images

This repository defines the requirements for Ceph images used by Ceph testcontainers modules, and provides a checker that verifies whether a given image meets them. It also extracts lighter role images from the official image.

## Purpose

Ceph testcontainers modules use the official Ceph image by default:

```text
quay.io/ceph/ceph:v20.2.4@sha256:6bb1c8a42fbc0bf87938946990b65174466997bc11c31eb5a323225a779fd8f9
```

Two needs can call for a different image:

- Size: the official image ships components that tests never use, and every container pulls all of them.
- Custom builds: tests may need to run against a patched or internally built Ceph instead of the official release.

With size in mind, the requirements are split by role: `control`, `osd`, `rgw` and `mds`, so each container pulls only what it runs. An image that holds every component (`all`) is also allowed, and it can still be much lighter than the official image as long as it meets the requirements.

The requirements keep the components that tests depend on and leave out those that only operations need, such as the dashboard. How a custom image is built is up to its owner; this repository defines the requirements and checks images against them. For the size need alone, role images can be extracted directly from the official image.

Cluster setup always belongs to the testcontainers module, which supplies configuration, keyrings, bootstrap commands and entrypoints at run time. Images carry no setup logic, which is why the official image works unmodified.

## Layout

```text
docs/IMAGE_REQUIREMENTS.md   Image requirements and the checker reference
docs/ROLE_IMAGES.md          Role images extracted from the official image
image/check.py               Checker for any local image
image/check-runtime.sh       Quick check probe, run inside each image
image/functional/            Functional scenarios used by the full check
image/roles/                 Role image extraction from the official image
```

Unit tests live in `tests/` next to each tool. Reports and logs go to the ignored `artifacts/` directory.

## Roles

| Role | Contents |
| --- | --- |
| `control` | MON, MGR, CLI and Python clients, RBD and CephFS mirror daemons |
| `osd` | OSD, BlueStore, object classes and plugins |
| `rgw` | RGW and its admin CLI |
| `mds` | CephFS MDS |
| `all` | Every component of the four roles in one image |

See [Image Requirements](docs/IMAGE_REQUIREMENTS.md) for the exact requirements and where each component is used.

## Checking an image

Requires Python 3.9 or later and a Docker engine with API 1.49 or later. The checker never builds or pulls images; prepare them locally first.

| Level | Verifies |
| --- | --- |
| Quick (default) | Each image contains the required components |
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

The output is `ceph-testcontainers:<release>-<role>` for the four roles and `all`. See [Role Images](docs/ROLE_IMAGES.md).

## Development

`make check` runs the host unit tests of the checker, the functional harness and the role extraction. [The workflow](.github/workflows/test.yml) runs them, then pulls the official image and runs the quick and the full check against it.

## License

The code and documents in this repository are licensed under the [MIT License](LICENSE).

The checker and the functional scenarios run Ceph only inside containers and include no Ceph code. Role images extracted with `image/roles/` are different: they contain Ceph and distribution packages from the official image, each under its own license (Ceph is mostly LGPL-2.1 or LGPL-3, see its [COPYING](https://github.com/ceph/ceph/blob/v20.2.4/COPYING)). The extraction keeps their license files in every image. Whoever distributes such images must meet those licenses, including making the corresponding source available where required.
