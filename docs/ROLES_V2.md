# Dependency roles, version 2

This opt-in contract follows the execution/dependency analysis in issue #3.
Legacy control/osd/mds/rgw tags and checker mode keep their meanings.

| Role | Required capability |
| --- | --- |
| essential | MON, MGR, OSD; ceph CLI, keyring/monmap bootstrap, process inspection, release always-on MGR modules and volumes/rbd_support/mirroring/snap_schedule with dependencies; BlueStore, rbd/rgw/cephfs/hello/lock classes, compressor and erasure-code plugins |
| client | ceph/rados/rbd; Python rados/rbd/cephfs, ceph_argparse/ceph_daemon; libradosstriper, librbd LUKS1/LUKS2, libcryptsetup and cryptsetup executable; command/process utilities |
| cephfs | MDS and cephfs-mirror, their libraries and local ceph CLI |
| rbd-aux | rbd-mirror and cache-capable client runtime: rados/rbd CLI/bindings, ceph-immutable-object-cache and the librbd parent_cache plugin, local ceph CLI |
| rgw | RGW Beast/TLS, radosgw-admin and readlink; ceph/Python runtime for independently invoked per-zone setup containers |
| all | Union of the five responsibilities |

Common requirements in IMAGE_REQUIREMENTS.md continue to apply. MGR modules
and OSD classes stay inside their host daemon. Libraries can be shared across
roles. Ordinary RBD needs essential + client, not rbd-aux. The cache daemon,
plugin and consuming librbd client share the cache client's container/socket.
RGW setup uses the rgw runtime with an idle entrypoint; gateway-local admin
operations stay in the gateway. Client does not require radosgw-admin.

Images do not start services or contain cluster setup logic. The core image
is reused by separate daemon containers. Optional MGR modules are enabled by
the consumer when needed. Python in a client cannot supervise commands inside
another container; custom targets retain explicit capability checks.

## Building and checking

```sh
make roles-v2-images V2_VARIANT=official CEPH_RELEASE=19.2.5
make roles-v2-images V2_VARIANT=debian CEPH_RELEASE=19.2.5
make roles-v2-images V2_VARIANT=ubuntu CEPH_RELEASE=19.2.5
make roles-v2-images V2_VARIANT=alpine  # independent 19.2.4 experiment

python3 image/check.py --contract roles-v2 --full \
  --image essential=ceph-testcontainers:roles-v2-official-19.2.5-essential \
  --image client=ceph-testcontainers:roles-v2-official-19.2.5-client \
  --image cephfs=ceph-testcontainers:roles-v2-official-19.2.5-cephfs \
  --image rbd-aux=ceph-testcontainers:roles-v2-official-19.2.5-rbd-aux \
  --image rgw=ceph-testcontainers:roles-v2-official-19.2.5-rgw \
  --image all=ceph-testcontainers:roles-v2-official-19.2.5-all
```

Installed RPM/deb/APK closures are resolved offline from the complete pinned
source runtime. Ceph executable placement is explicit even when a distribution
package bundles several daemons. License files, corresponding-source notices,
package inventory and documented Alpine modifications remain in the outputs.
Identical membership-group tar layers are reused, including in all. Source
runtime CEPH_ARGS and architecture-specific tcmalloc settings are retained.

The roles-v2 workflow builds all supported release/variant/platform cells.
Manual full runs validate split roles and all separately before optional
publication. Publisher stage/promote/health take `--contract roles-v2`; output
tags begin `roles-v2-`, and legacy reports cannot promote into that namespace.
The six-platform completeness and exact tested-image gates remain in force.

## Existing requirement and test relocation

R01–R28 refer in order to IMAGE_REQUIREMENTS.md's component-use table.

| Existing requirements | New quick-test owner | Functional follow-up |
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
| R16–R19 MGR modules | essential | HEALTH_OK, volumes/tasks/mirroring; consumer scheduling/quiesce tests |
| R20 RBD mirror | rbd-aux; setup in client | rbd-snapshot-mirror; consumer journal/failover/later-daemon tests |
| R21 CephFS mirror | cephfs; MGR module in essential | cephfs-snapshot-mirror; consumer peer/MGR topology tests |
| R22 RGW multisite administration | rgw setup runtime | rgw-multisite, independently with no generic client image |
| R23–R24 OSD/classes/plugins | essential | cluster-lifecycle, hello/lock; consumer compressor/EC operations |
| R25–R27 RGW/admin/listeners | rgw | rgw-s3; consumer TLS/Swift/STS/admin/host-listener tests |
| R28 MDS | cephfs | cephfs; consumer standby/replacement/failover tests |

The scenario-to-role manifest in image/roles/contract.py covers all eleven
existing image scenarios. Selected runs resolve only that scenario union;
missing roles fail before Docker allocation. Full split and all runs are
independent. Neither all nor control is an implicit missing-role substitute.

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
v2 distribution runtime, with license-file exceptions. Executable pruning,
metadata removal and layer reuse contribute alongside role selection; savings
must not be attributed to role labels alone.
