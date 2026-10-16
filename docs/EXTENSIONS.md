# Extension Images

## Purpose

Every baseline module feature works with the unmodified official Ceph image, and the [image requirements](IMAGE_REQUIREMENTS.md) define what a replacement must provide for those features. An extension is different: it adds tools that the official image does not ship, for a module feature that a caller selects explicitly.

- Normal cluster use never needs an extension image. Selecting one does not change the role requirements or the role images.
- The caller names the image to use for the extension. The module then checks that the image meets the extension's contract below, and separately that the runtime grants what the feature needs, before it changes anything. A tag name or a label alone is not accepted as proof.
- A missing tool, a denied permission and an unsupported kernel are distinct errors. Nothing is installed at run time, and there is no fallback to another mechanism.
- How an extension image is built is up to its owner. The contract names executables and behavior, not packages, labels or file paths. [image/extension_check.py](../image/extension_check.py) checks any local image against it.

This repository defines the contracts, checks images against them and publishes one image per extension. The modules decide when an extension is used and own everything the feature does with it.

## netem helper (contract 1)

The netem helper delays packets in the kernel, below Ceph. A module starts the helper in the network namespace of a target container (Docker network mode `container:<target>`) and grants `NET_ADMIN` to the helper alone. The target keeps its own image and capabilities, so the official image, the role images and custom Ceph images can all be targets. The helper contains no Ceph component.

Contract 1 covers delay and jitter on the egress of the target's interface on a private bridge network.

### Image contract

| Requirement | Detail |
| --- | --- |
| Platform | Linux `arm64` or `amd64`. The container runs as root. |
| Entry | The module overrides entrypoint and command; `sleep infinity` keeps an idle helper running. |
| Executables on `PATH` | `sh`, `sleep`, `tc`, `ip`, `python3` |
| Versions | `tc -V` and `ip -V` report an iproute2 version. |
| JSON queries | `tc -j -s qdisc show`, `tc -j -s filter show dev <interface>`, `ip -j link show`, `ip -j address show` and `ip -j route show` print valid JSON. |
| netem | `tc` parses `qdisc add ... netem delay <time> <jitter>`. Without `NET_ADMIN`, the kernel refuses the request with `Operation not permitted`; `tc` must not fail earlier because it lacks netem. |
| Command supervision | `python3` imports `base64`, `ctypes`, `json`, `os`, `select`, `signal`, `subprocess`, `sys` and `time`, and `prctl(PR_SET_CHILD_SUBREAPER)` succeeds through `ctypes`. Modules run every `tc` command under a Python supervisor that they can cancel. |
| Filesystem | `/tmp` is writable. |
| Runtime closure | Each executable loads with its libraries and data. |

The published helper also carries the labels `io.ceph-testcontainers.extension=netem` and `io.ceph-testcontainers.extension.contract=1`. Other helpers do not need them, and the checker does not rely on them.

### Runtime requirements

These belong to the engine and the target, not to the image. A module checks them before it applies a delay.

- `NET_ADMIN` granted to the helper container. Without it, every change fails with `Operation not permitted`.
- The kernel's netem qdisc (`sch_netem`), built in or loadable on the engine's kernel. Without it, `tc` reports `Specified qdisc kind is unknown`.
- A target on a private bridge network whose namespace the helper can join. A delay on a host-network target would shape the host, so modules refuse those targets, as well as stopped targets and targets that share another container's namespace.

### Native behavior to plan for

The functional check below verifies these on every engine it runs on.

- Removing the helper container does not remove the qdisc from a live namespace. Remove the qdisc first.
- When the target restarts, Docker gives it a new namespace (a new `SandboxID`) without the qdisc. A helper that is still running stays in the old namespace, where the target's interface no longer exists.
- The delay applies to packets that leave through the target's interface. A request and its answer cross it once each way, so a TCP request to a delayed target takes at least the delay, typically twice the delay (the SYN-ACK and the answer).

## Checking an extension image

Requires Python 3.9 or later and a Docker engine with API 1.49 or later. The checker never builds or pulls images.

```sh
make extension-images                     # build ceph-testcontainers:extension-netem
make extension-check                      # check it against the default release's official image

python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev \
  --functional --target quay.io/ceph/ceph:v20.2.4 --target my-registry/ceph-osd:dev
```

| Level | Verifies |
| --- | --- |
| Quick (default) | The image contract, in a container with no network and no capabilities |
| Functional (`--functional`) | The quick check, then the runtime on this engine: netem added, changed and removed on the loopback of a disposable namespace with `NET_ADMIN`, and the delay scenario for each `--target` |

The delay scenario uses a private bridge network:

1. It starts a container of the target image, a server and a helper with `NET_ADMIN` in the target's namespace, and a client container on the same network.
2. It finds the target's interface by its address and MAC address, and records its root qdisc.
3. It times requests before the delay, adds a 200 ms netem delay (`--delay-ms`) with its own handle, and times them again. The median must reach the delay, and netem must count packets.
4. It changes the delay to half with a tenth of jitter, removes the helper, and checks from a new helper that the qdisc is unchanged.
5. It removes the qdisc, checks that the root qdisc is the original one again, and times requests once more.
6. It adds the delay again and restarts the target. The target must get a new network sandbox, the helper left in the old namespace must no longer see the target's interface, and a new helper must find the original root qdisc.
7. It removes every container and network it created. Cleanup passes only when the final inventory succeeds and is empty.

`extension-report.json` records the image identity and labels, every check, the engine (`docker info`), the kernel, each target's image ID, interface and namespace, the timings, the netem counters, and the cleanup. A failure carries a kind: `image_unsupported`, `permission_denied`, `kernel_unsupported`, `runtime_failure`, `timeout` or `checker_failure`.

### Candidate selector profiles

`--selector` adds a check of a peer-selective profile to a functional check. The profiles are candidates: contract 1 does not promise them, a published helper is not checked against them before publication, and a module must not rely on them until a contract does.

```sh
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev \
  --functional --target quay.io/ceph/ceph:v20.2.4 --selector ipv4-tcp-u32 --selector ipv6-tcp-u32
```

| Profile | Selects |
| --- | --- |
| `ipv4-tcp-u32` | The target's IPv4 egress to one peer address, TCP, one destination port |
| `ipv6-tcp-u32` | The same over IPv6, for TCP directly after the IPv6 base header |

Each profile uses the same owned hierarchy, installed root first and filter last, so that until the filter exists all traffic takes the unshaped band:

```text
root prio 7e00:, 3 bands, every priority mapped to band 1 (7e00:1)
child netem 7e10:, parent 7e00:3
u32 filter, preference 10, handle 800::1: peer/32 or /128, TCP, destination port 7000 -> 7e00:3
```

The scenario uses a private dual-stack bridge network with the target, two peers that answer on ports 7000 and 7001 over both families, a helper with `NET_ADMIN` and a request driver in the target's namespace:

1. It finds the target's interface by its address and MAC, records its root qdisc, and times the selected path (peer B, the profile's family, port 7000).
2. It installs the hierarchy with a delay (`--delay-ms`) and reads the filter back. tc prints one `match` member per u32 key, repeated in one object, so the checker decodes them in order instead of as a dictionary, and compares every offset, value, mask and offmask, the handle and the flow ID with the requested selector. tc packs keys into words: the IPv6 next header and port appear at offsets 4 and 40.
3. It times four controls before any selected traffic: peer B over the other family, peer B on port 7001, the third peer on port 7000, and a reverse connection from peer B to the target. They must stay fast, and the netem child must count no packet. Then the selected path must take at least the delay, and the child must count its packets.
4. It waits, within 2 s, until the child holds no packet (backlog and queue length 0): a completed read does not show that the queue drained. Then it changes the child to 100% loss. A fresh selected connection must time out, the controls must still answer, and the child must count drops.
5. It removes the leaf first, then whatever of its u32 table remains (removing the last leaf can remove its whole chain), then the root. The root qdisc must be the original one again, no filter may remain, and the selected path must be fast again.

`extension-report.json` keeps each profile's result under `selectors`, and each scenario directory keeps the raw filter dump (`filter.json`) and every timing. The `ipv6-tcp-u32` profile does not select IPv6 traffic with extension headers or fragments, and neither profile covers IPv4 options, IPv4-mapped addresses or receiver-side shaping.

CI runs both profiles on the GitHub-hosted runners of both architectures, against the same targets as the contract check, in a job that no publication waits for. On Docker Desktop 29.8.2 (LinuxKit 7.0.14, arm64), both profiles passed against the official image and the OSD role image of 20.2.4 and 19.2.5, with the published `extension-netem-v1` helper. With a 200 ms delay, the selected path took 410 to 412 ms and every control 0.06 to 0.11 ms; the child drained within 170 ms.

## Published helper

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-netem-v1
```

The tag lists `linux/amd64` and `linux/arm64`; `extension-netem-v1-linux-<architecture>` selects one platform. The contract version is part of the tag, so a later incompatible contract gets a new tag.

- Built from [image/extensions/netem](../image/extensions/netem) on the pinned `debian:bookworm-slim` base of the Debian role images, with Debian's `iproute2` and `python3` (iproute2 6.1.0 and Python 3.11.2 when this was written). The `linux/arm64` image is 45 MB compressed and 155 MB unpacked.
- `/usr/share/ceph-testcontainers/SOURCES.txt` and `source-packages.txt` list every package with its source package and version.
- CI checks the helper functionally on native runners of both architectures, with the official image and the published OSD role image of every release in [image/releases.py](../image/releases.py) as targets. A manual run with `publish` uploads the exact tested images, promotes the tag only when both architectures passed in the same run, and then checks the published digests again the same way.
- The helper is checked and published apart from the release images. Its failure never blocks them, and theirs never blocks it.
- The weekly published image health workflow also runs `python3 image/publish.py extension-health`, which fails when the helper tag no longer lists both platform images.

### Engine coverage

| Engine | Platforms | Evidence |
| --- | --- | --- |
| Docker Engine on GitHub-hosted Ubuntu 24.04 runners | `amd64`, `arm64` | Functional check before every publication and on the published digests |
| Docker Desktop 29.8.2 on macOS, LinuxKit kernel 7.0.14 | `arm64` | Functional check with the official 20.2.4 and 19.2.5 images and their published OSD role images |

Other engines, rootless Docker, Podman and Docker Desktop's Enhanced Container Isolation are not covered. Run the functional check on them before relying on the helper.

### Why a helper and not extended role images

The other way to provide `tc` is to add it to the role images and run `tc` inside each daemon container.

- Every role image of every variant and release would need the tools and a separate netem tag, and an extended `all` image would have to stay the union of the extended roles.
- Every daemon container that could be a target would need `NET_ADMIN`, and the module would have to grant it to every initial and later MON, MGR, OSD, RGW, MDS, mirror and client container.
- The official image and custom Ceph images could never be targets.

The helper avoids all three, at the cost of one more small image and one more container per delayed target. No case seen so far needs the role-image route.

## Not in contract 1

- Packet loss, duplication, reordering and rate limits. They use the same `tc`, but their kernel behavior is not checked yet.
- Peer-selective delay with classifiers, receiver-side delay through IFB, and IPv6 networks. The [candidate selector profiles](#candidate-selector-profiles) are checked, but not promised.
- `ss`, `nft` and `tcpdump` for socket destruction, peer-specific filtering and packet capture. Each becomes its own contract with its own checks.
