# Image Requirements

## Purpose

The role images are the default runtime for Ceph testcontainers. The upstream
official image remains a complete aggregate compatibility reference for every
release in [image/releases.py](../image/releases.py). All baseline features
must work with that image unmodified.

Opt-in extension features, which a caller selects explicitly, may use an additional extension image with tools that the official image does not ship. They never change these requirements; see [Extension Images](EXTENSIONS.md).

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

A role describes runtime contents, not container count. MON, MGR and OSD
run in separate containers from essential. Optional features select their
explicit role; only an explicit all selection supplies every role.

| Role | Required capability |
| --- | --- |
| essential | MON, MGR, OSD; ceph CLI, keyring/monmap bootstrap, process inspection, release always-on MGR modules and volumes/rbd_support/mirroring/snap_schedule with dependencies; BlueStore, rbd/rgw/cephfs/hello/lock classes, compressor and erasure-code plugins |
| client | ceph/rados/rbd and offline CRUSH/OSD map diagnostics (`crushtool`, `osdmaptool`); Python rados/rbd/cephfs, ceph_argparse/ceph_daemon; libradosstriper, librbd LUKS1/LUKS2, libcryptsetup and cryptsetup executable; command/process utilities |
| cephfs | MDS, cephfs-mirror and the offline journal tool `cephfs-journal-tool`, their libraries and local ceph CLI |
| rbd-aux | rbd-mirror and cache-capable client runtime: rados/rbd CLI/bindings, ceph-immutable-object-cache and the librbd parent_cache plugin, local ceph CLI |
| rgw | RGW Beast/TLS, radosgw-admin and readlink; ceph/Python runtime for independently invoked per-zone setup containers |
| all | Union of the five responsibilities |

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

The [role capability table](ROLES.md) specifies executables, libraries and
bindings. MGR modules and OSD classes/plugins remain inside essential.
Client includes ordinary CLI/bindings, encryption/striper clients and offline
map tools. The cache daemon, parent-cache plugin and consuming librbd stay
together in rbd-aux. RGW administration belongs to rgw; CephFS daemons and
mirror runtime belong to cephfs. No optional role falls back to essential.

## Where each component is used

The table lists what a testcontainers module does with each component. Rows marked `common` apply to every role. Each component is part of the official image. A role image keeps every component of its role even when a test does not exercise the feature, so that it can replace the official image in every topology.

| Component | Role | Used for |
| --- | --- | --- |
| `sh`, `mkdir`, `cp`, `cat`, `rm`, `awk` | common | Bootstrap scripts that write configuration, keyrings and daemon data |
| `hostname -i` | `essential` | MON bootstrap and MON join read the container address for the monmap |
| `test` | common | File and readiness checks executed directly in a running container |
| `sleep infinity` | `essential` | Entrypoint of client, CLI and tools containers that stay idle while commands are executed in them |
| `ceph-mon`, `monmaptool` | `essential` | Creating the initial monmap, starting MONs and adding or replacing MONs |
| `ceph-authtool` | `essential` | Generating and reading the admin, daemon and client keyrings |
| `ceph-mgr` | `essential` | Active and standby MGRs, health and placement group readiness |
| `ceph` with `ceph_argparse`, `ceph_daemon`, Python `rados` | `client` | Every cluster command the module issues; the `ceph` CLI imports these modules |
| `rados` and libradosstriper | `client` | RADOS object I/O, striped put/get/rm, pool inspection and CephFS data pool checks |
| `rbd` | `client` | RBD images, snapshots, clones, namespaces, backup export and import, and RBD mirroring configuration |
| Python `rados`/`rbd`, librbd encryption, `cryptsetup` | `client` | Userspace encrypted RBD creation/read and external LUKS passphrase changes on regular-file raw exports; RADOS compound class calls and locking |
| Python `cephfs` | `client` | Userspace CephFS I/O helpers, such as directory pinning and clone identity checks |
| `python3` | `essential/client` | RGW HTTP and TLS readiness probes, S3, RBD and CephFS client helpers |
| `ceph-immutable-object-cache`, librbd `parent_cache` plugin | `rbd-aux` | The RBD immutable object cache: the daemon runs in a client container and caches parent-image objects of clones for librbd clients there that enable `rbd_parent_cache_enabled` |
| `pgrep` | `essential/client` | Process inspection in the control container: `pgrep -f` finds a running command by its command line, for example to confirm that a canceled `ceph` command no longer runs |
| MGR always-on modules | `essential` | Required for `HEALTH_OK`; a missing module or dependency raises a health error |
| MGR `volumes` | `essential` | CephFS filesystems, subvolumes, snapshots, clones, data pools and quiesce |
| MGR `rbd_support` with Python `rbd` | `essential` | RBD background tasks and mirror snapshot schedules |
| MGR `mirroring` | `essential` | CephFS snapshot mirroring peers and directory configuration |
| `rbd-mirror` | `rbd-aux` | RBD journal and snapshot mirroring daemon, started in its own container when RBD mirroring between clusters is requested |
| `cephfs-mirror` | `cephfs` | CephFS snapshot mirroring daemon, started in its own container when CephFS mirroring between clusters is requested |
| `radosgw-admin` | `rgw` | RGW multisite realm, zonegroup, zone, period and sync policy management, executed from a client container |
| `ceph-osd` with object classes and plugins | `essential` | OSDs; RBD, RGW and CephFS operations call the object classes inside the OSD, and pools load the compressor and erasure-code plugins |
| OSD `hello` and `lock` object classes | `essential` | Built-in class requests and striper locking, with each class’s runtime dependencies |
| `radosgw` | `rgw` | RGW gateways for S3, Swift and STS |
| `radosgw-admin` | `rgw` | RGW users, keys, buckets and placement created from the gateway container |
| `readlink` | `rgw` | Confirming that the gateway owns the listening socket on host networks and TLS listeners |
| `ceph-mds` | `cephfs` | Active, standby and standby-replay MDS daemons |
| `cephfs-journal-tool` | `cephfs` | Offline MDS journal inspection, export and header backup, run from a cephfs container against a stopped or failed filesystem |

Daemons that are not requested are not started. An image with `rbd-mirror` does not start a mirror daemon in a single cluster, and an `all` image used for OSDs does not start MGR or RGW processes.

## Validation

[image/check.py](../image/check.py) verifies these requirements for any local
image. Quick checks verify required executable, library and class presence
and loading. Functional checks exercise real cluster operations. Missing
requirements fail validation; file presence or a successful `--version`
does not establish functional compatibility.

See [Test Scenarios](TEST_SCENARIOS.md) for checker commands, scenario steps,
pass criteria, validation scope and result interpretation.
