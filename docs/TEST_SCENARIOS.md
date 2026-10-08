# Test Scenarios

[image/check.py](../image/check.py) validates local Ceph images against the
[Image Requirements](IMAGE_REQUIREMENTS.md). Any builder or distribution
can be used; the checker requires executable capabilities, not package
names, build manifests or fixed library directories.

## Running the checker

The host needs Python 3.9 or later, a Docker CLI and engine, and Docker API
1.49 or later for platform inspection. Prepare images locally before running
the checker: it never builds or pulls them. No host Ceph SDK/cgo, kernel RBD
mapping, mounts or privileged containers are required.

```sh
# Quick: inspect one image's runtime requirements.
python3 image/check.py --image all=my-company/ceph:dev

# Full: quick prerequisites followed by every functional scenario.
python3 image/check.py --image all=my-company/ceph:dev --full

# Validate mixed roles and all independently in the same run.
python3 image/check.py \
  --image control=my-company/ceph:control --image osd=my-company/ceph:osd \
  --image rgw=my-company/ceph:rgw --image mds=my-company/ceph:mds \
  --image all=my-company/ceph:all --full
```

| Option | Behavior |
| --- | --- |
| `--image ROLE=REF` | Repeat for `control`, `osd`, `rgw`, `mds` or `all`; additional components are allowed |
| `--full` | Run every functional scenario after quick checks; requires `all` or all four role images |
| `--scenario NAME` | Repeat with `--full` to select scenarios; report level is `functional-selected` |
| `--platform PLATFORM` | Select `linux/arm64` or `linux/amd64` from a local multi-platform image |
| `--output-dir DIR` | Empty report directory; default `artifacts/image-check-<uuid>` |
| `--probe-timeout SECONDS` | Quick timeout per image; default 120 seconds |

References are resolved once to immutable local image IDs. Every supplied
image must use the same platform and Ceph release. Functional runs require
the selected platform to be the local default; prepare single-platform
images when checking a different architecture.

To select the encrypted RBD, object-class and striped I/O scenarios:

```sh
python3 image/check.py \
  --image control=my-company/ceph:control --image osd=my-company/ceph:osd \
  --image rgw=my-company/ceph:rgw --image mds=my-company/ceph:mds \
  --image all=my-company/ceph:all --full \
  --scenario rbd-encryption --scenario rados-object-class --scenario rados-striper
```

`image/functional/run.py` offers the same scenario selection and uses the
same prerequisites, immutable identities and report format.

## Quick checks

Quick checks run one disposable container per role, with no network or
volumes. `all` runs the checks of all four roles. Configuration queries
select MGR module, OSD class and plugin directories, so the checks follow
the image's effective paths rather than a distribution's fixed layout.

| Check | Roles | Action and verification |
| --- | --- | --- |
| Executable utilities | All | Find `sh`, `mkdir`, `cp`, `cat`, `rm`, `test`, `sleep`, `hostname` and `awk` as executable files on PATH, and check `/bin/sh`; establishes the bootstrap command prerequisites, including commands invoked directly through Docker exec |
| Writable Ceph paths | All | Create a test file in each configuration/data/run/log path and `/tmp`, copy it, read and compare its content, then remove it; verifies the paths support bootstrap file operations |
| New directories | All | Create a fresh directory below `/`, perform the same file operations and remove it; verifies the module can choose a new destination for copied files |
| Temporary-directory permissions | All | Check `/tmp` has its sticky bit; control also checks mode `1777` through Python; verifies the expected temporary-directory permissions |
| Hostname resolution | All | Run `hostname -i` with a disposable hostname mapping and require a nonempty address; verifies the address lookup used to build the monmap can run |
| Idle client process | All | Start `sleep infinity`, wait one second, require the process to remain alive, then terminate it; verifies an idle client container can stay available for later commands |
| Ceph executables | Each role | Run the required Ceph binaries with `--version` and parse release/commit; verifies executable startup and reported version consistency within the image and release consistency across the supplied role set |
| Python bindings | control | Import `rados`, `rbd`, `cephfs`, `ceph_argparse` and `ceph_daemon`; verifies the interpreter, bindings and import-time runtime dependencies load |
| Cryptsetup executable | control | Resolve the executable on PATH and run `cryptsetup --version`; verifies a runnable external command is available, independently of the shared library |
| Native client libraries | control | Resolve and eagerly load librbd, libcryptsetup and libradosstriper, then look up required encryption, keyslot and striper symbols; distinguishes missing libraries from dependency/symbol loading failures |
| Python client APIs | control | Require callable RBD encryption format/load, compound class execute, exclusive lock and unlock APIs; verifies the installed bindings expose the functions used by the functional probes |
| Striper CLI option | control | Run `rados --help` and require a successful result containing `--striper`; verifies the CLI advertises the required interface |
| MGR module files | control | Query `mgr_module_path` and require `mgr_module.py` and module files for volumes, rbd_support and mirroring; verifies those module sources are present, without claiming their cluster operations have run |
| Keyring creation and reading | control | Generate a temporary client keyring with `ceph-authtool`, then read its key back; verifies bootstrap keyring generation and parsing work |
| Monmap creation | control | Create a temporary monmap with a supplied FSID and v1/v2 MON addresses; verifies the monmap tool accepts the runtime bootstrap inputs |
| Mirror/admin executables | control | Run `rbd-mirror`, `cephfs-mirror` and `radosgw-admin` with `--version`; verifies startup and version identity, while replication is checked in separate functional scenarios |
| OSD object-class files | osd | Query `osd_class_dir` and locate the rbd, rgw, cephfs, hello and lock shared objects; verifies the required class files exist |
| OSD object-class loading | osd | Preload each required class into `ceph-osd` with eager ELF relocation, run `--version` and reject loader errors even if the process exits zero; verifies class dependency and OSD symbol resolution, without claiming class methods executed |
| OSD plugin files | osd | Query `plugin_dir` and require shared objects in the compressor and erasure-code groups; verifies plugin files are present, without claiming every plugin has loaded or processed data |
| Gateway prerequisites | rgw | Require `readlink` on PATH and run `radosgw`/`radosgw-admin` version checks; verifies required gateway/admin executables start and the socket-inspection utility is available |
| Metadata-server prerequisite | mds | Run `ceph-mds --version`; verifies the MDS executable and its startup dependency closure load |

OSD object-class loading does not require Python or a compiler in the OSD
image. Class method execution, encrypted I/O and striper data operations
are verified by their functional scenarios.

A quick PASS proves these prerequisites. Functional PASS requires the
cluster operations below; file presence and `--version` do not qualify.

## Functional scenarios

Scenarios run real clusters on private Docker networks. Single-cluster
scenarios share one cluster; each multi-cluster scenario starts its own
pair. MON/MGR and clients use control, OSDs use osd, gateways use rgw, and
metadata servers use mds. For `all`, every container uses that image.

| Scenario | Clusters | Pass criteria |
| --- | --- | --- |
| `cluster-lifecycle` | 1 | MON/MGR and two OSDs bootstrap; RADOS data survives OSD restart, addition and removal; `HEALTH_OK` |
| `rbd` | 1 | Python RBD I/O; snapshot, protect, clone, flatten and parent removal preserve expected content |
| `cephfs` | 1 | MDS/filesystem startup; userspace write, fsync, rename and unlink across sessions; MGR volumes subvolume |
| `rgw-s3` | 1 | RGW/user setup; SigV4 bucket/object operations; wrong and anonymous credentials rejected |
| `rbd-encryption` | 1 | Both LUKS formats support creation, fresh-client reads and external passphrase changes with identity, size, data and ciphertext invariants below |
| `rados-object-class` | 1 | Compound hello call reaches OSD, executes and returns the expected result and object data |
| `rados-striper` | 1 | Multiple shards, exact put/get data, successful rm with no objects remaining, and actual lock/unlock |
| `rbd-backup` | 2 | Full and incremental export-diff/import-diff content hashes match |
| `rbd-snapshot-mirror` | 2 | Peer bootstrap and snapshot mirroring replicate two update rounds |
| `cephfs-snapshot-mirror` | 2 | Mirroring peer bootstrap and directory snapshot replication succeed |
| `rgw-multisite` | 2 | Realm/zones/period setup, realm pull and object replication to the secondary zone succeed |

### `cluster-lifecycle`: cluster bootstrap and OSD changes

Uses control and osd images in one cluster.

1. Generate a fresh FSID, monmap and CephX keyrings, initialize MON, then
   start MGR and two file-backed BlueStore OSDs. Require an available MGR,
   the expected number of OSDs `up` and `in`, and `active+clean` placement
   groups. This verifies that the images can initialize and operate a
   cluster using configuration and identity supplied at runtime.
2. Create a RADOS pool, put a known object with the CLI, then get it and
   compare all bytes. This verifies client authentication and the initial
   client-to-OSD read/write path.
3. Restart an existing OSD, wait for OSD readiness and clean placement
   groups, then read the object again. The bytes must remain identical,
   demonstrating that restarting the daemon preserves its stored data.
4. Add a newly initialized OSD, wait for the new OSD count and clean
   placement groups, then mark that OSD out, wait for clean placement,
   stop it and purge its cluster record. Require the remaining OSDs to be
   ready and placement groups clean. This verifies OSD registration,
   placement updates and controlled removal.
5. Read the original object again and require identical bytes and
   `HEALTH_OK`. The completed membership changes must leave a working
   cluster with the original data accessible.

This covers controlled OSD lifecycle operations. It does not exercise MON
quorum failover or permanent loss of all replicas.

### `rbd`: snapshots, clones and independent image data

Uses control and osd images in one cluster.

1. Create and initialize an RBD pool, create a 32 MiB parent image through
   Python librbd, write and flush a 4 MiB payload, and hash the entire
   image. This establishes a known image state through userspace RBD I/O.
2. Create and protect a parent snapshot, then clone it into a child image.
   Write another 1 MiB payload to the parent at offset 8 MiB. The child's
   whole-image SHA-256 must still equal the parent hash taken before that
   update, proving the clone reads the snapshot state rather than the
   changed parent head.
3. Flatten the child, unprotect and remove the parent snapshot, then hash
   the child again. Its content must remain identical, proving flattening
   preserves data after the snapshot dependency is removed.
4. Remove the parent image and hash the child once more. The unchanged
   hash proves the flattened child remains readable without its parent.

Each write checks the returned byte count and flushes; hash probes use
fresh Python clients and read the entire image, including unwritten areas.

### `cephfs`: file persistence and MGR subvolumes

Uses control, osd and mds images in one cluster.

1. Create metadata and data pools, create a filesystem and start MDS.
   Require an `up:active` MDS and clean placement groups, verifying that
   the metadata service can serve the filesystem.
2. Mount through Python LibCephFS, create a directory, write a 1 MiB file,
   require a complete write and fsync it. Rename the file and require the
   old path to return a not-found result. This exercises file data,
   persistence and namespace updates through the userspace client.
3. Close that filesystem session and mount a fresh one. Read the renamed
   file and compare every byte with the original payload. This verifies
   the persisted file is visible outside the writer's session.
4. Unlink the file, require its path to be absent, and remove its directory.
   This verifies file and directory removal through MDS.
5. Create a subvolume with `ceph fs subvolume create`, obtain its path,
   verify that path is a directory, then write and read back a 64 KiB file
   in separate client processes. This verifies the MGR volumes API creates
   a usable subvolume and its path supports normal CephFS I/O.

These mounts use LibCephFS inside control containers; no kernel mount is
used. The scenario does not exercise MDS failover.

### `rgw-s3`: authenticated object operations

Uses control, osd and rgw images in one cluster.

1. Start RGW and wait for an HTTP service response, then create a user
   and credentials with `radosgw-admin`. This verifies gateway startup and
   administrative user provisioning.
2. Send a SigV4-signed bucket creation request and require HTTP 200.
   Put a 1 MiB object and get it back, requiring HTTP 200 and identical
   bytes. This verifies authenticated S3 writes and reads through RGW
   to its backing OSD storage.
3. List the bucket with ListObjectsV2 and require the object's key in the
   response. This verifies the object is also visible through listing.
4. Attempt to read with an incorrect secret, then without credentials.
   Both must return HTTP 403, proving a private object's read path rejects
   those requests rather than merely accepting valid signatures.
5. Delete the object and require HTTP 204, then require HTTP 404 on a
   subsequent authenticated get. Delete the empty bucket and require
   HTTP 204. This verifies deletion and the object's resulting absence.

The scenario covers these S3 operations over HTTP; it does not validate
TLS, Swift, STS or every S3 API.

### `rbd-encryption`: encrypted RBD and external passphrase change

Uses control and osd images in one cluster.

For each of LUKS1 and LUKS2:

1. Create a 64 MiB RBD, format AES-256 encryption, reopen the image with
   encryption loaded, then write and flush a 2 MiB payload at logical
   offset 1 MiB. This verifies the Python binding and librbd encryption
   runtime can create and write the requested LUKS format.
2. Close the original Image and Rados connection. Open a fresh client with
   the original passphrase and verify the payload and logical size. This
   proves a newly opened client can decrypt persisted data without the
   original client's loaded encryption state.
3. Close native clients and export the raw RBD to a temporary regular file.
   Record image ID, raw/logical size and the SHA-256 of all bytes outside
   the encryption header. Verify the raw LUKS magic/version and that raw
   payload bytes differ from plaintext. The header boundary is raw size
   minus logical size; these observations establish the raw encrypted
   state against which the external change is checked.
4. Run `cryptsetup luksChangeKey` on the file using temporary key files.
   Require an actual header change and unchanged file size and ciphertext.
   This verifies the external executable can replace the passphrase while
   leaving encrypted data outside the header untouched.
5. Write only changed header blocks back to the same raw RBD. Verify its
   ID/raw size, the applied header and the entire outside-header ciphertext.
   The updated image must remain the original RBD, with no writes beyond
   the header boundary and no change to its encrypted data.
6. Open fresh clients: the old passphrase must fail with native EPERM; the
   new passphrase must succeed with the original logical size and payload.
   Other native errors do not count as passphrase rejection. This verifies
   the changed header takes effect and preserves application-visible data.
7. Remove fixture images and verify none remain, proving the image cleanup
   completes for both formats.

This tests compatibility for applications using encrypted RBD and external
passphrase changes. It does not test a Ceph rekey API or full data
reencryption. The fixture's reduced cryptsetup PBKDF iteration count keeps
tests short; it is not a production password policy.

### `rados-object-class`: compound calls and OSD responses

Uses control and osd images in one cluster.

1. Create a RADOS pool and submit a Python librados compound write
   operation containing `hello.record_hello` with input `application`.
   Successful operation completion exercises the client API, request
   transport and OSD execution of the built-in class method.
2. Call `hello.replay` on that object. Python `Ioctx.execute` must return
   a byte count of 19 and exactly `Hello, application!`. This verifies
   the response from the executed OSD class reaches the client intact.
3. Read the object directly and require the same bytes. This verifies
   `record_hello` actually stored the greeting, rather than only producing
   a response without the expected data mutation.
4. Remove the fixture objects and require an empty object listing,
   verifying their cleanup.

This proves the request reaches OSD and its built-in class loads, executes
and responds. It does not guarantee arbitrary user-defined class
compatibility. The client belongs in control; hello and its dependencies
belong in osd.

### `rados-striper`: striped I/O and OSD locks

Uses control and osd images in one cluster.

1. Create a lock fixture and take an exclusive lock through Python
   librados. A second cookie must receive `ObjectBusy` while the first
   holds it. Unlock the first cookie, acquire with the second, unlock
   again and remove the fixture. This verifies actual lock-class
   acquisition, conflict detection, release and subsequent acquisition.
2. Put a 9 MiB payload with `rados --striper`, larger than its default
   4 MiB object size. List the pool and require at least three objects,
   all with the striped fixture's prefix. This verifies the CLI and
   libradosstriper split one logical object into multiple RADOS shards.
3. Get the logical object with `rados --striper` and compare the full
   downloaded file with the source. Identical bytes prove the shards are
   reassembled into the original data, including the final partial shard.
4. Remove it with `rados --striper rm` and require an empty pool listing
   before fallback cleanup. This verifies striper deletion itself removes
   every shard rather than relying on later fixture removal.

This exercises libradosstriper and OSD together, including split, reassembly,
deletion and lock-class execution. Control provides the CLI and striper
library; osd provides lock and its dependencies. No `cls_striper` is required.

### `rbd-backup`: full and incremental image restore

Uses control and osd images in two independent clusters.

1. Initialize RBD pools in the source and backup clusters. Create a
   32 MiB source image, write and flush 4 MiB at offset zero, and create
   snapshot `one`. Write another 2 MiB at offset 16 MiB and create
   snapshot `two`. These provide two known image states with a change
   between them.
2. Export snapshot `one` with `rbd export-diff` without a starting
   snapshot, then export `two` with `--from-snap one`. This exercises
   generation of a full initial diff and its incremental successor.
3. Transfer both diff files through the host to the backup cluster and
   create a separate 32 MiB destination image. Import the initial diff
   and compare the entire restored `one` snapshot's SHA-256 with the
   source `one` snapshot. Equality verifies full restoration across
   separate cluster storage.
4. Import the incremental diff and compare the entire restored `two`
   snapshot with source `two`. Equality verifies the subsequent changes
   can be applied without losing the earlier image state.

This tests export/import-based restoration. It does not start a mirror
daemon or test automatic backup scheduling.

### `rbd-snapshot-mirror`: snapshot replication across clusters

Uses control and osd images in two independent clusters. The mirror daemon
runs from the secondary control image.

1. Initialize both RBD pools and enable image-mode mirroring with distinct
   site names. Create a peer bootstrap token on the primary and import it
   on the secondary with `rx-only`. This exercises peer configuration and
   the secondary client's authenticated contact with the primary.
2. Create a CephX identity with mirror permissions and start `rbd-mirror`
   on the secondary with access to both cluster networks. This verifies
   the control image can run the mirror daemon with injected credentials.
3. Create a 32 MiB primary image and enable snapshot mirroring. Write
   and flush a 2 MiB payload, create a mirror snapshot and wait until
   the secondary whole-image SHA-256 equals the primary hash. This
   verifies the initial replicated image becomes readable with matching
   data on the receiving cluster.
4. Write and flush a second 2 MiB payload at offset 12 MiB, create another
   mirror snapshot and wait for a second whole-image hash match. This
   verifies the running daemon propagates later updates as well as the
   initial image.
5. Query the secondary image's mirror status and require an `up+` state,
   confirming the daemon reports the replicated image as up.

This covers one-way snapshot mirroring over two updates. It does not
exercise journal mirroring, promotion, failover or reverse replication.

### `cephfs-snapshot-mirror`: directory snapshot replication

Uses control, osd and mds images in two independent clusters. The mirror
daemon runs from the primary control image.

1. Create a filesystem and start an active MDS in each cluster. Enable
   the MGR mirroring module on both and filesystem snapshot mirroring
   on the primary. This exercises the module and filesystem APIs needed
   to configure replication.
2. Create a peer bootstrap token on the secondary, import it on the
   primary, and give the primary MGR access to the secondary network.
   This verifies the mirroring module can establish the remote peer
   relationship through its bootstrap API.
3. Create a CephX identity with mirror permissions and start
   `cephfs-mirror` on the primary with access to both clusters. This
   verifies the control image can run the userspace mirror daemon with
   the configured identity and peer connectivity.
4. Write and fsync a 1 MiB file beneath `/mirrored`, register that
   directory for mirroring, and create snapshot `first` in its `.snap`
   directory. This supplies persisted file data and a snapshot for
   the daemon to discover and replicate.
5. Repeatedly open a userspace filesystem client on the secondary and
   read `/mirrored/.snap/first/file` until it exactly matches the source
   payload. This verifies directory and snapshot metadata arrive with
   the replicated file data and are readable through the secondary MDS.

This checks one directory snapshot. It does not test ongoing live-file
synchronization, multiple update rounds or failover.

### `rgw-multisite`: realm configuration and cross-zone object replication

Uses control, osd and rgw images in two independent clusters. Gateway and
administrative containers communicate over a shared peer network.

1. On the primary, create a realm, a master zonegroup and master zone
   with the primary gateway endpoint. Create a system user with sync
   credentials and commit the period, then start the primary RGW in that
   zone. This exercises multisite administration and activation of the
   primary realm configuration.
2. Pull the realm on the secondary using the primary gateway and system
   credentials. Create a secondary zone with its endpoint and commit its
   period, then start the secondary RGW in that zone. This verifies realm
   discovery, authenticated administrative communication and registration
   of the receiving zone.
3. Create a normal user on the primary, then create a bucket and put a
   known 1 MiB object through signed S3 requests to the primary gateway.
   This exercises the source zone's authenticated data path after
   multisite configuration.
4. Repeatedly issue a signed get against the secondary gateway with
   that user's credentials until it returns HTTP 200 and the exact
   original payload. This verifies that the user credentials, bucket
   and object become usable in the other zone and its gateway serves
   the replicated object bytes.

This covers primary-to-secondary replication of a newly created object.
It does not test bidirectional conflicts, deletion replication, failover
or every multisite sync policy.

## Reports and compatibility claims

Each run writes `check-report.json`, quick logs per role and functional
Docker logs, scenario proofs and failed-container logs. The report includes
input references, runnable and platform image IDs, available registry
digests, platform, image environment, Ceph release/commit, source hashes,
requested scenarios, failure stages and cleanup results.

| Result | Meaning |
| --- | --- |
| `missing_file` | A required executable, library, Python module or class is absent |
| `loading_failure` | A runtime component or its dependency/symbol closure cannot load |
| `unsupported_feature` | A required executable option is unavailable |
| `quick_failure` | Another prerequisite, such as a writable path or MGR module check, failed |
| `timeout` / `checker_failure` | The quick probe timed out or could not produce a valid result |
| `functional_failure` | A real cluster operation or data invariant failed |
| `cleanup_failure` | Resource removal or absence verification failed |
| `not_run` | Functional checks were blocked by failed prerequisites; not a PASS |

Missing dependencies on supported paths fail validation and are never
hidden by a skip. Exit code 0 means success, 1 means a failed check, and 2
means invalid input. A report applies only to its image IDs, platform and
requested scenarios; `functional-selected` is not a full-suite claim.

Every fixture container/network has a session label and is removed at the
end, including after failures. Remaining resources fail validation. Data
proofs include encrypted image identity/sizes/header boundaries/ciphertext
hashes, hello return bytes, striper shard names/hashes and fixture cleanup.

The validation matrix checks Debian and Ubuntu on ARM64 and AMD64, with
mixed roles and `all` independently. The unchanged pinned Quay image is the
comparison baseline. [CI](../.github/workflows/test.yml) uses native runners
and retains per-image-set reports.
