# Role Images from the Official Image

[image/roles/build.py](../image/roles/build.py) extracts the five role images `control`, `osd`, `rgw`, `mds` and `all` from the official Ceph image. It copies the official binaries and their package dependencies into new images and never compiles Ceph. The result is a ready-made lighter alternative to the official image, and a reference showing that the [image requirements](IMAGE_REQUIREMENTS.md) can be met. Custom builds are out of scope; their owners build them and verify them with the checker.

## Host requirements

- Python 3.9 or later
- Docker Engine with API 1.49 or later and a compatible CLI (`docker image inspect --platform` is used)
- BuildKit or buildx with `ADD --link`

## Usage

```sh
make role-images          # extract, smoke test and run the quick check
make role-images-full     # extract, smoke test and run the full check

python3 image/roles/build.py --source-image quay.io/ceph/ceph:v20.2.4 --platform linux/arm64 --check quick
```

`make` uses `CEPH_SOURCE_IMAGE` (default: the pinned official image in the [Makefile](../Makefile)) and `ROLE_REPOSITORY` (default `ceph-testcontainers`).

| Option | Behavior |
| --- | --- |
| `--source-image IMAGE` | Official Ceph image, by tag or digest; required |
| `--repository NAME` | Output repository, default `ceph-testcontainers` |
| `--tag PREFIX` | Output tag prefix; defaults to the release reported by `ceph --version` |
| `--platform PLATFORM` | Target platform; defaults to the source image architecture |
| `--check quick` | After the build, run the quick image check on the five images |
| `--check full` | After the build, run the full image check for the mixed role set and for `all` |
| `--runtime-env NAME=VALUE` | Add an environment variable to every output image; repeatable |
| `--output-dir DIR` | Report directory; must be missing or empty |
| `--skip-pull` | Use the cached source image |
| `--skip-smoke` | Skip smoke tests; recorded as `skipped` |
| `--keep-context` | Copy the generated build context into the report directory |

## Output images

Images are tagged `<repository>:<prefix>-<role>`, for example `ceph-testcontainers:20.2.4-control`. They are loaded into the local Docker engine. Nothing is pushed to a registry, and no multi-platform manifest is created; each run builds one platform.

| Role | Root packages |
| --- | --- |
| `control` | `ceph-mon`, `ceph-mgr`, `ceph-common`, `python3-cephfs`, `rbd-mirror`, `cephfs-mirror` |
| `osd` | `ceph-osd` |
| `rgw` | `ceph-radosgw` |
| `mds` | `ceph-mds` |
| `all` | Union of the four roles |

Every role also receives a shell, core utilities, `hostname`, `gawk`, CA certificates and the base filesystem package. Each role contains the RPM dependency closure of its roots, resolved offline from the source image's package database. Because `ceph-common` is a dependency of every Ceph daemon package, the `ceph`, `rados`, `rbd` and `radosgw-admin` clients and the Python bindings are present in every role. Packages that only operations need, such as the dashboard, disk prediction, cephadm and compilers, are not in any closure.

Each image contains `/usr/share/ceph-testcontainers/image-manifest.json` and `runtime-packages.txt`, which record the role, source image digest, architecture, Ceph version, root packages, package versions and file groups.

## Assembly and layers

The rootfs is assembled inside a container started from the source image with no network. The result is passed to the host as tar files.

- Included: package files of the closure, `%license` files and Ceph's `COPYING`, passwd, group, NSS configuration, loader cache, CA trust, `/etc/alternatives`, Ceph configuration, data, log and socket directories.
- Excluded: documentation, man and info pages, Python bytecode caches.
- Preserved: numeric UID and GID, file modes, symlinks and hardlinks. Timestamps are normalized to zero. Runtime directories are set to `0755` and `/tmp` to `1777`.
- Not preserved: extended attributes, so capabilities, ACLs and SELinux labels are dropped. The images are meant for root containers.

Files are partitioned by role membership into disjoint tar groups, and each group is added with `ADD --link` so roles share identical layers:

| Group | Used by |
| --- | --- |
| `common` | `control`, `osd`, `rgw`, `mds` |
| `shared-control-osd` | `control`, `osd` |
| `control`, `osd`, `rgw`, `mds` | The role of the same name |
| Role manifest | One per role |

`all` is built from every payload group. After the build, the builder checks that each shared group has the same layer DiffID in every image that uses it.

## Runtime environment

`--runtime-env NAME=VALUE` adds a literal `ENV` to all five images. Names must match `[A-Za-z_][A-Za-z0-9_]*`; duplicates and control characters are rejected, and `NAME=` sets an empty value. `$`, quotes and backslashes are kept as given, so quote the argument to avoid shell expansion. After the build, each image's `Config.Env` must contain exactly the requested values. No environment variable is added by default.

## Verification

| Step | Checks |
| --- | --- |
| Smoke | Extraction quality per role: manifests and package list, ownership and mode probes, architecture, expected executables present, other roles' daemons absent, Python imports, versions equal to the manifest, keyring and monmap creation for `control`, OSD object classes and plugins |
| `--check quick` | The [quick image check](IMAGE_REQUIREMENTS.md#quick-check), the same one used for any other image |
| `--check full` | The [full image check](IMAGE_REQUIREMENTS.md#full-check) with the mixed role images and with `all` |

## Report

Each run writes to a new `artifacts/roles-<UTC>-<UUID>/` directory unless `--output-dir` is given.

| File | Content |
| --- | --- |
| `build-report.json` | Source identity and platform, Ceph version, output tags, IDs and sizes, shared DiffIDs, runtime environment, status of each step and errors |
| `source-image.json`, `plan.json` | Source image inspection; packages, file groups and role manifests |
| `Dockerfile.generated`, `assembly.log`, `build-<role>.log` | Generated role Dockerfile, assembly and build logs |
| `smoke-<role>.log` | Smoke test output |
| `image-check.log`, `image-check/` | Image check output and its `check-report.json` |
| `context/` | Build context, with `--keep-context` |

Each step is recorded as `passed`, `failed`, `skipped`, `not_run` or `not_requested`.
