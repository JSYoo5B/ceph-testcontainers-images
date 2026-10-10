# Image Requirements

## Purpose

The official Ceph image is the default for Ceph testcontainers modules, and every module feature must work with it unmodified:

```text
quay.io/ceph/ceph:v20.2.4@sha256:6bb1c8a42fbc0bf87938946990b65174466997bc11c31eb5a323225a779fd8f9
```

The same requirements apply to every release in [image/releases.py](../image/releases.py); the official image of each listed release must meet them unmodified.

Other images are used when the official image is too large for the tests at hand, or when tests must run against a custom Ceph build. This document defines what such an image must provide. Any image that meets these requirements can replace the official image, and [image/check.py](../image/check.py) verifies a given image against them.

The requirements include every component that tests depend on and exclude components that only operations need. The official image also ships, for example:

- the MGR dashboard and its frontend
- disk prediction and the machine learning libraries it pulls in
- cephadm, ceph-volume and LVM tooling
- NFS Ganesha and iSCSI gateways
- compilers and development headers

None of these is required. Images may still contain them, or any other additional executable.

The testcontainers module supplies all cluster setup at run time: configuration, keyrings, bootstrap commands and entrypoints. Images carry no setup logic. Any build method, distribution, package manager, file layout or label scheme is acceptable; building the image is up to its owner. For a ready-made option, [role images](ROLE_IMAGES.md) can be extracted from the official image.

## Roles

Requirements are split by role so that each container pulls only what it runs. A role describes the components an image must contain, not the number of containers: MON and MGR, for example, run as separate containers from the same `control` image.

| Role | Purpose |
| --- | --- |
| `control` | MON, MGR, CLI clients, mirror daemons and client containers |
| `osd` | OSD daemons, one container per OSD |
| `rgw` | RGW gateways |
| `mds` | CephFS metadata servers |
| `all` | Every component of the four roles; one image reused by all containers, like the official image |

Roles without their own image fall back to the `control` image. A cluster always needs `control` and `osd`; `rgw` and `mds` are required only when RGW or CephFS is started.

## Common requirements

These apply to every role. The official image meets all of them.

- Platform: Linux `arm64` or `amd64`. Containers run as root.
- Entry: the module overrides entrypoint and command. Images must not depend on cephadm, systemd or an image-specific entrypoint, and daemons run in the foreground.
- Shell and utilities: `/bin/sh` and the core utilities `mkdir`, `cp`, `cat`, `rm`, `test`, `sleep`, `hostname` and `awk` are executables on `PATH`, as in the official image. `hostname -i` prints the container address, and `sleep infinity` keeps an idle container running.
- Filesystem: the standard Ceph paths `/etc/ceph`, `/var/lib/ceph`, `/var/run/ceph` (a symlink to `/run/ceph` is fine) and `/var/log/ceph` are writable, `/tmp` is sticky (normally `1777`), and new directories can be created for files the module copies in.
- Runtime closure: every role executable loads, including its ELF loader, shared libraries, Python ABI and imports, and every module or plugin the daemon loads at runtime. Package names may differ between distributions.
- Identity: the image contains no cluster. FSID, CephX keys, daemon IDs, addresses and ports are injected at run time, and daemons start from empty data directories.
- Networking: daemons accept addresses supplied at run time on both bridge and Linux host networks. No address, port or endpoint is fixed in the image.
- Storage: OSDs initialize with `--mkfs` on a sparse BlueStore file. Block devices, LVM, privileged mode and kernel mounts are not used.
- Versions: Ceph executables report `ceph version <release> (<commit>)` for `--version`. All Ceph executables in one image report the same release and commit, and images combined in one cluster report the same release.

## Role requirements

| Role | Executables on `PATH` | Additional runtime dependencies |
| --- | --- | --- |
| `control` | `ceph-mon`, `ceph-mgr`, `ceph`, `ceph-authtool`, `monmaptool`, `rados` supporting `--striper`, `rbd`, `cryptsetup`, `rbd-mirror`, `cephfs-mirror`, `radosgw-admin`, `python3` | Python modules `rados`, `rbd`, `cephfs`, `ceph_argparse`, `ceph_daemon`; librbd LUKS1/LUKS2 encryption, libcryptsetup and libradosstriper with their runtime dependencies; the release's always-on MGR modules plus `volumes`, `rbd_support` and `mirroring`, each with its Python dependencies |
| `osd` | `ceph-osd` | BlueStore; `rbd`, `rgw`, `cephfs`, `hello` and `lock` object classes with their runtime dependencies; compressor and erasure-code plugins |
| `rgw` | `radosgw`, `radosgw-admin`, `readlink` | Beast HTTP frontend with TLS and the RGW shared libraries |
| `mds` | `ceph-mds` | MDS shared libraries |
| `all` | Union of the four roles | Union of the four roles |

Python, the CLI tools of other roles, compilers and development headers are not required in `osd`, `rgw` or `mds`.

`all` includes every control and OSD requirement. `cryptsetup` must be an executable
on `PATH`: libcryptsetup alone cannot perform the external passphrase-change
command. The `hello` and `lock` classes belong to OSD; their clients belong to
control. Striped I/O uses libradosstriper and `lock`, without a separate
`cls_striper`. Package names and distribution-specific library paths are not
part of the contract. The system linker must resolve the required libraries
and their dependencies; Ceph's effective `osd_class_dir` selects the classes.

## Where each component is used

The table lists what a testcontainers module does with each component. Rows marked `common` apply to every role. Each component is part of the official image. A role image keeps every component of its role even when a test does not exercise the feature, so that it can replace the official image in every topology.

| Component | Role | Used for |
| --- | --- | --- |
| `sh`, `mkdir`, `cp`, `cat`, `rm`, `awk` | common | Bootstrap scripts that write configuration, keyrings and daemon data |
| `hostname -i` | `control` | MON bootstrap and MON join read the container address for the monmap |
| `test` | common | File and readiness checks executed directly in a running container |
| `sleep infinity` | `control` | Entrypoint of client, CLI and tools containers that stay idle while commands are executed in them |
| `ceph-mon`, `monmaptool` | `control` | Creating the initial monmap, starting MONs and adding or replacing MONs |
| `ceph-authtool` | `control` | Generating and reading the admin, daemon and client keyrings |
| `ceph-mgr` | `control` | Active and standby MGRs, health and placement group readiness |
| `ceph` with `ceph_argparse`, `ceph_daemon`, Python `rados` | `control` | Every cluster command the module issues; the `ceph` CLI imports these modules |
| `rados` and libradosstriper | `control` | RADOS object I/O, striped put/get/rm, pool inspection and CephFS data pool checks |
| `rbd` | `control` | RBD images, snapshots, clones, namespaces, backup export and import, and RBD mirroring configuration |
| Python `rados`/`rbd`, librbd encryption, `cryptsetup` | `control` | Userspace encrypted RBD creation/read and external LUKS passphrase changes on regular-file raw exports; RADOS compound class calls and locking |
| Python `cephfs` | `control` | Userspace CephFS I/O helpers, such as directory pinning and clone identity checks |
| `python3` | `control` | RGW HTTP and TLS readiness probes, S3, RBD and CephFS client helpers |
| MGR always-on modules | `control` | Required for `HEALTH_OK`; a missing module or dependency raises a health error |
| MGR `volumes` | `control` | CephFS filesystems, subvolumes, snapshots, clones, data pools and quiesce |
| MGR `rbd_support` with Python `rbd` | `control` | RBD background tasks and mirror snapshot schedules |
| MGR `mirroring` | `control` | CephFS snapshot mirroring peers and directory configuration |
| `rbd-mirror` | `control` | RBD journal and snapshot mirroring daemon, started in its own container when RBD mirroring between clusters is requested |
| `cephfs-mirror` | `control` | CephFS snapshot mirroring daemon, started in its own container when CephFS mirroring between clusters is requested |
| `radosgw-admin` | `control` | RGW multisite realm, zonegroup, zone, period and sync policy management, executed from a client container |
| `ceph-osd` with object classes and plugins | `osd` | OSDs; RBD, RGW and CephFS operations call the object classes inside the OSD, and pools load the compressor and erasure-code plugins |
| OSD `hello` and `lock` object classes | `osd` | Built-in class requests and striper locking, with each class’s runtime dependencies |
| `radosgw` | `rgw` | RGW gateways for S3, Swift and STS |
| `radosgw-admin` | `rgw` | RGW users, keys, buckets and placement created from the gateway container |
| `readlink` | `rgw` | Confirming that the gateway owns the listening socket on host networks and TLS listeners |
| `ceph-mds` | `mds` | Active, standby and standby-replay MDS daemons |

Daemons that are not requested are not started. An image with `rbd-mirror` does not start a mirror daemon in a single cluster, and an `all` image used for OSDs does not start MGR or RGW processes.

## Validation

[image/check.py](../image/check.py) verifies these requirements for any local
image. Quick checks verify required executable, library and class presence
and loading. Functional checks exercise real cluster operations. Missing
requirements fail validation; file presence or a successful `--version`
does not establish functional compatibility.

See [Test Scenarios](TEST_SCENARIOS.md) for checker commands, scenario steps,
pass criteria, validation scope and result interpretation.
