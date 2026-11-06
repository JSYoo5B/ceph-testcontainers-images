# Role images

This default production contract follows the execution/dependency analysis in issue #3.

| Role | Required capability |
| --- | --- |
| essential | MON, MGR, OSD and the offline BlueStore tool `ceph-bluestore-tool`; ceph CLI, keyring/monmap bootstrap, process inspection, release always-on MGR modules and volumes/rbd_support/mirroring/snap_schedule with dependencies; BlueStore, rbd/rgw/cephfs/hello/lock classes, compressor and erasure-code plugins |
| client | ceph/rados/rbd and offline CRUSH/OSD map diagnostics (`crushtool`, `osdmaptool`); Python rados/rbd/cephfs, ceph_argparse/ceph_daemon; libradosstriper, librbd LUKS1/LUKS2, libcryptsetup and cryptsetup executable; command/process utilities |
| cephfs | MDS, cephfs-mirror and the offline journal tool `cephfs-journal-tool`, their libraries and local ceph CLI |
| rbd-aux | rbd-mirror and cache-capable client runtime: rados/rbd CLI/bindings, ceph-immutable-object-cache and the librbd parent_cache plugin, local ceph CLI |
| rgw | RGW Beast/TLS, radosgw-admin and readlink; ceph/Python runtime for independently invoked per-zone setup containers |
| all | Union of the five responsibilities |
| mgr-modules | Optional replacement MGR process image: the baseline MGR of essential plus additional MGR modules; see [The mgr-modules role](#the-mgr-modules-role) |

Common requirements in IMAGE_REQUIREMENTS.md continue to apply. MGR modules
and OSD classes stay inside their host daemon; the optional mgr-modules image
carries the MGR daemon with its modules. Libraries can be shared across
roles. Ordinary RBD needs essential + client, not rbd-aux. The cache daemon,
plugin and consuming librbd client share the cache client's container/socket.

The client quick check creates and prints an OSD map, exports its CRUSH map,
decodes/recompiles it, then verifies exact CRUSH bytes after import/export.
The package closure includes the map-tool owner package; unrelated daemon
executables remain excluded. Essential retains its bootstrap `crushtool`.

The essential role carries `ceph-bluestore-tool` for offline operations on a
stopped OSD's store, for example `bluefs-bdev-expand` after a sparse block file
was lengthened. It comes from the same package as `ceph-osd` on the RPM and deb
sources, so no package is added there. The Alpine experiment installs
`ceph19-osd-tools`, which holds it; the role filter keeps the other offline OSD
tools in that package out of every role.

The cephfs role carries `cephfs-journal-tool` for offline MDS journal
inspection, export and header backup. It comes from the same ceph-common
package as the local CLI, so no package is added; the role filter keeps it out
of essential, client, rbd-aux and rgw. `cephfs-table-tool` and
`cephfs-data-scan` are not selected. The Alpine experiment omits the tool
because its source package keeps it in the separate utils package.

The client retains `ceph-dencoder` when the source provides it (supported
RPM/deb ceph-common packages). The Alpine experiment's upstream source lacks
this diagnostic CLI, so its contract does not claim that capability.
RGW setup uses the rgw runtime with an idle entrypoint; gateway-local admin
operations stay in the gateway. Client does not require radosgw-admin.

Images do not start services or contain cluster setup logic. The core image
is reused by separate daemon containers. Optional MGR modules are enabled by
the consumer when needed. Python in a client cannot supervise commands inside
another container; custom targets retain explicit capability checks.

## The mgr-modules role

mgr-modules replaces only the MGR process image of a cluster. MON and OSD
stay on essential, and every other daemon keeps its own role. It is a MGR
capability superset of essential, not a filesystem superset: it need not
contain ceph-mon, ceph-osd, ceph-bluestore-tool, ceph-mds, radosgw or the
mirror daemons. It is optional and is not part of all, so the five roles and
all are unchanged by it. Its contract has three separate parts.

### Baseline role

Any image used as mgr-modules must provide, like essential's MGR:

- `ceph-mgr` with its ELF and Python runtime, and the local CLI `ceph` and
  `ceph-conf`. Its release and commit must match the cluster's other role
  images; the checker refuses a role set with different releases.
- The launch path the module injects: `/bin/sh`, `mkdir` and `cp` for the
  MGR script, writable `/etc/ceph`, `/var/lib/ceph`, `/var/run/ceph`,
  `/var/log/ceph` and `/tmp`, supplied networking and a caller-controlled
  entrypoint. Readiness runs `test -S` on the admin socket. The other common
  requirements of [Image Requirements](IMAGE_REQUIREMENTS.md) apply too.
  Admin socket commands to the MGR run from the control container through
  the shared run directory, not inside the MGR image.
- The baseline modules: the release's always-on modules (balancer, crash,
  devicehealth, orchestrator, pg_autoscaler, progress, rbd_support, status,
  telemetry and volumes for Squid, Tentacle and Umbrella, as `ceph mgr module
  ls` reports them on each release's MON) and volumes, rbd_support,
  mirroring and snap_schedule, each with `mgr_module.py` and the transitive
  Python and module dependencies. These keep cluster health and the RBD,
  CephFS and RGW fixtures working when the MGR runs from this image.

The same baseline is the MGR requirement of essential and all.

### Project bundle

The project-built mgr-modules image is extracted from the official image
only (the `official` variant). It holds every MGR module that the selected
official release ships, with the runtime closure of the packages that ship
them: `ceph-mgr`, `ceph-common` and each installed package that owns a module
directory with a `module.py`. For Tentacle 20.2.4 these are the 29 module
directories of `ceph-mgr-modules-core` plus dashboard, cephadm,
diskprediction_local, k8sevents and rook (34 modules from
`ceph-mgr-modules-core`, `ceph-mgr-dashboard`, `ceph-mgr-cephadm`,
`ceph-mgr-diskprediction-local`, `ceph-mgr-k8sevents` and `ceph-mgr-rook`). The
inventory follows the release: Squid 19.2.5 also ships 34 modules, with
restful and zabbix instead of Tentacle's nvmeof and smb.
Other Ceph executables are excluded as in every role. The bundle keeps
`cephadm`, the executable the cephadm module reads at load time
(`cephadm_path`).

The inventory is derived from the installed packages at extraction and is
recorded, with the owning package of every module, in the image's
`image-manifest.json` (`mgr_modules`, `mgr_module_packages`) and in the
`io.ceph-testcontainers.mgr-modules` label. Package versions, source packages
and license files are recorded as for every role. The inventory is
build-specific evidence of what is installed; it is not a claim that every
module is operational.

The module payload dominates its size. Measured with
`image/roles/measure.py` for Tentacle 20.2.4 on linux/amd64, mgr-modules is
280.8 MiB of gzip layers and 838.9 MiB unpacked, against 517.7 MiB and
1447.8 MiB for the official image and 160.3 MiB and 423.4 MiB for essential.
A cluster with MON and OSD on essential and MGR on mgr-modules pulls
302.9 MiB of distinct compressed layers. Dashboard (148 MiB), the
diskprediction_local models (66 MiB) and SciPy/NumPy are the largest parts.

The Debian, Ubuntu and Alpine variants build no mgr-modules image. Their
module packages would need their own pinned inputs, and the role's bundle is
defined by the official release.

### Requested capabilities and custom images

A custom mgr-modules image, such as the official image itself or a consumer
build with extra modules, needs the baseline and only the modules its tests
request. It need not carry Dashboard or the project bundle's module list.
`image/check.py --image mgr-modules=REF --mgr-module NAME` requires each
named module in addition to the baseline. Any valid module name is accepted,
including modules unknown to this repository; there is no allowlist. A
requested module that is absent, or whose dependency closure cannot import,
fails the check with that module's name, and the check never substitutes
another image. The project bundle is checked with its complete recorded
inventory, and publication requires that check.

### Installation is not enablement

Images enable nothing. Which modules are enabled stays in the MON's MgrMap:
the release defaults plus whatever a caller or a feature extension enables
and configures itself. Installing Dashboard, Prometheus, alerts, influx,
telegraf, telemetry reporting, cephadm, rook or k8sevents does not activate
them, open a port, send data or contact an external service.

### Functional limitations

The quick check proves module files, their Python import closure and
selected assets (Dashboard's frontend build, the cephadm executable). It does
not prove that a module works. Modules that need external systems remain
unsupported by this image alone: cephadm, rook and k8sevents need managed
hosts or a Kubernetes cluster, and the orchestrator-backed parts of
Dashboard and nfs need an orchestrator; diskprediction_local needs device
health data; alerts, influx, telegraf and prometheus need an SMTP, InfluxDB,
Telegraf or scrape receiver. Module functionality belongs to the
corresponding feature scenarios of the consumer.

The check requires a module's unconditional imports at their full dotted
path. `import a.b.c` needs the submodule `a.b.c`, not only the package `a`,
and `from a.b import c` needs `a.b` and a name `c` that is either an
attribute of `a.b` or its importable submodule. Imports of the module tree
itself cannot run outside the MGR, so their dotted module paths are checked
on disk. A dependency that a module imports inside an import fallback is
optional to the check, because
the module itself then reports that it cannot run. The official Tentacle
20.2.4 image ships the influx module without the `influxdb` Python client,
so in a running MGR influx reports "influxdb python module not found", and
the project bundle keeps that parity with its source. The cluster-lifecycle
scenario records such modules (`cannot_run`) as evidence; a consumer that
needs one supplies an image with its dependency.

## Building and checking

```sh
make role-images VARIANT=official CEPH_RELEASE=19.2.5
make role-images VARIANT=debian CEPH_RELEASE=19.2.5
make role-images VARIANT=ubuntu CEPH_RELEASE=19.2.5
make role-images VARIANT=alpine  # independent 19.2.4 experiment

python3 image/check.py --full \
  --image essential=ceph-testcontainers:official-19.2.5-essential \
  --image client=ceph-testcontainers:official-19.2.5-client \
  --image cephfs=ceph-testcontainers:official-19.2.5-cephfs \
  --image rbd-aux=ceph-testcontainers:official-19.2.5-rbd-aux \
  --image rgw=ceph-testcontainers:official-19.2.5-rgw \
  --image all=ceph-testcontainers:official-19.2.5-all
```

The official build also produces `official-<release>-mgr-modules` and checks
it with `--mgr-module` for every module of its recorded inventory:

```sh
python3 image/check.py \
  --image essential=ceph-testcontainers:official-19.2.5-essential \
  --image mgr-modules=ceph-testcontainers:official-19.2.5-mgr-modules \
  $(python3 -c 'import json; print(" ".join("--mgr-module " + m for m in json.load(open("artifacts/official-19.2.5-linux-amd64/plan.json"))["roles"]["mgr-modules"]["mgr_modules"]))')
```

Installed RPM/deb/APK closures are resolved offline from the complete pinned
source runtime. Ceph executable placement is explicit even when a distribution
package bundles several daemons. License files, corresponding-source notices,
package inventory and documented Alpine modifications remain in the outputs.
Identical membership-group tar layers are reused, including in all. Source
runtime CEPH_ARGS and architecture-specific tcmalloc settings are retained.

The images workflow builds every available catalogued release/variant/platform
cohort that it is asked for ([Releases](RELEASES.md)). The image requirement
check gates optional publication; the functional scenarios, which validate
split roles and all separately, run in the same workflow and are recorded as
auxiliary evidence without blocking it. Publisher stage/promote/health use the
default role contract; tags are `<variant>-<release>-<role>`. Promotion
requires every available cohort of a release and publishes only the exact
images that passed the requirement check.

## Requirements and validation

R01–R28 refer in order to IMAGE_REQUIREMENTS.md's component-use table.

| Requirements | Quick-check role | Functional coverage |
| --- | --- | --- |
| R01–R04 common/bootstrap utilities | Every role | Bootstrap and cleanup in every selected scenario |
| R05–R08 MON/MGR/bootstrap/core CLI | essential; local CLI subset in each relevant role | cluster-lifecycle; consumer MON/MGR topology and auth tests |
| R09 RADOS/striper | client; OSD lock in essential | rados-striper and actual lock/shard proofs |
| R10 RBD | client; OSD class in essential | rbd, rbd-backup |
| R11 native encryption/class clients | client; OSD hello/lock in essential | rbd-encryption, rados-object-class |
| R12 CephFS clients | client | cephfs; consumer pin/clone metadata tests |
| R13 Python probes | Actual probe runtime | HTTP/S3 and native client probes |
| R14 parent cache | rbd-aux, effective plugin directory without MGR | Consumer native cache/lease tests; quick loading alone is insufficient |
| R15 process inspection | essential/client; other runtime only where required | Native command cancellation and process-observer tests |
| R16–R19 MGR modules | essential; mgr-modules with its requested modules | HEALTH_OK, volumes/tasks/mirroring; the mgr-modules topology; consumer scheduling/quiesce tests |
| R20 RBD mirror | rbd-aux; setup in client | rbd-snapshot-mirror; consumer journal/failover/later-daemon tests |
| R21 CephFS mirror | cephfs; MGR module in essential | cephfs-snapshot-mirror; consumer peer/MGR topology tests |
| R22 RGW multisite administration | rgw setup runtime | rgw-multisite, independently with no generic client image |
| R23–R24 OSD/classes/plugins | essential | cluster-lifecycle, hello/lock; consumer compressor/EC operations |
| R25–R27 RGW/admin/listeners | rgw | rgw-s3; consumer TLS/Swift/STS/admin/host-listener tests |
| R28 MDS | cephfs | cephfs; consumer standby/replacement/failover tests |

With `--full`, a supplied mgr-modules image adds the `mgr-modules` topology:
the mixed roles with every MGR started from mgr-modules. `--topology` selects
topologies. The images workflow runs cluster-lifecycle, rbd and cephfs in
that topology as auxiliary evidence; cluster-lifecycle records the MGR's
always-on, enabled and non-runnable modules.

The scenario-to-role manifest in image/roles/contract.py covers all eleven
existing image scenarios. Selected runs resolve only that scenario union;
missing roles fail before Docker allocation. Full split and all runs are
independent. Missing optional roles are errors; all is selected explicitly.

Core-only/RBD scenarios never start mirror or gateway processes. RGW-only
scenarios use core probes and an RGW setup executor. Independent native client
oracles use client; cache oracles use rbd-aux. Keep data/hash/auth/lifecycle
invariants and failure classification when changing test placement.

Eleven image scenarios do not prove native cache use, scheduling, daemon
failover or every protocol. Those retain explicit downstream Go coverage.
Build, quick, selected functional, full functional and consumer results remain
separate evidence. Remeasure final manifests/layers; the previous size PoC
included RGW administration in the ordinary client and is not this contract's
final size result. Image size does not establish startup/CPU/RAM savings.


## Measuring final images

`image/roles/measure.py --image essential=REF --image client=REF ...
--baseline OLD_ALL_REF --output artifacts/layers.json` freezes local image
identities and counts each exported layer blob once per deployment. It verifies
unpacked DiffIDs against Docker metadata and records each blob's encoding.
Only an all-gzip result is a compressed-layer comparison. It does not measure
registry/config/protocol overhead, physical snapshot storage or CPU/RAM.

The existing assembler's doc/man/info/bytecode exclusions also apply to the
distribution runtime, with license-file exceptions. Executable pruning,
metadata removal and layer reuse contribute alongside role selection; savings
must not be attributed to role labels alone.
