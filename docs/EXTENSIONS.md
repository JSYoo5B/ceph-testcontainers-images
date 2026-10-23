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

Contract 1 covers delay and jitter on the egress of the target's interface on a private bridge network, and, through its [selector profiles](#selector-profiles), delay and loss of only the packets to selected peers, families and TCP ports. Its optional [bandwidth profile](#bandwidth-profile) adds netem's rate and queue limit to those selected packets, and its optional [packet-fault profile](#packet-fault-profile) adds netem's reorder, duplicate and corrupt. Each optional profile is selected and checked on its own. A helper that meets contract 1 without them stays valid for callers that only delay or drop packets, and a helper with the bandwidth profile alone stays valid for callers that only add a rate.

### Image contract

| Requirement | Detail |
| --- | --- |
| Platform | Linux `arm64` or `amd64`. The container runs as root. |
| Entry | The module overrides entrypoint and command; `sleep infinity` keeps an idle helper running. |
| Executables on `PATH` | `sh`, `sleep`, `tc`, `ip`, `python3` |
| Versions | `tc -V` and `ip -V` report an iproute2 version. |
| JSON queries | `tc -j -s qdisc show`, `tc -j -s filter show dev <interface>`, `ip -j link show`, `ip -j address show` and `ip -j route show` print valid JSON. |
| netem | `tc` parses `qdisc add ... netem delay <time> <jitter>`. Without `NET_ADMIN`, the kernel refuses the request with `Operation not permitted`; `tc` must not fail earlier because it lacks netem. |
| Selectors | `tc` parses `qdisc add ... prio bands 3 priomap ...` and `filter add ... u32` with `match ip` and `match ip6` keys for `dst`, `protocol` and `dport`. Without `NET_ADMIN`, the kernel refuses each with `Operation not permitted`. |
| Rate (bandwidth profile only) | `tc` parses `qdisc add ... netem rate <rate>bit limit <packets>`, alone and after `delay <time>`. Without `NET_ADMIN`, the kernel refuses it with `Operation not permitted`; a `tc` that cannot parse the rate fails earlier with `What is "rate"?`. |
| Packet faults (packet-fault profile only) | `tc` parses `qdisc add ... netem delay <time> <jitter> <correlation> reorder <percent> <correlation> duplicate <percent> <correlation> corrupt <percent> <correlation>`, and `delay 0us 0us 0% duplicate <percent> <correlation>`. Without `NET_ADMIN`, the kernel refuses each with `Operation not permitted`; a `tc` that cannot parse a fault fails earlier, as with `What is "corrupt"?`. |
| Command supervision | `python3` imports `base64`, `ctypes`, `json`, `os`, `select`, `signal`, `subprocess`, `sys` and `time`, and `prctl(PR_SET_CHILD_SUBREAPER)` succeeds through `ctypes`. Modules run every `tc` command under a Python supervisor that they can cancel. |
| Filesystem | `/tmp` is writable. |
| Runtime closure | Each executable loads with its libraries and data. |

The published helper also carries the labels `io.ceph-testcontainers.extension=netem` and `io.ceph-testcontainers.extension.contract=1`. Other helpers do not need them, and the checker does not rely on them.

### Runtime requirements

These belong to the engine and the target, not to the image. A module checks them before it applies a delay.

- `NET_ADMIN` granted to the helper container. Without it, every change fails with `Operation not permitted`.
- The kernel's netem qdisc (`sch_netem`), built in or loadable on the engine's kernel. Without it, `tc` reports `Specified qdisc kind is unknown`.
- For the bandwidth profile, a netem qdisc that keeps the rate and the queue limit it is given and reports them back through `tc -j`. Otherwise the checker reports `kernel_unsupported`.
- For the packet-fault profile, a netem qdisc that keeps each fault's probability and correlation and the reorder gap, and reports them back through `tc -j`. Otherwise the checker reports `kernel_unsupported`.
- For the selector profiles, the kernel's prio qdisc (`sch_prio`) and u32 classifier (`cls_u32`). Without them, `tc` reports `Specified qdisc kind is unknown` or `TC classifier not found`. An image cannot provide a kernel module, so the checker reports these as `kernel_unsupported`.
- A target on a private bridge network whose namespace the helper can join. A delay on a host-network target would shape the host, so modules refuse those targets, as well as stopped targets and targets that share another container's namespace.

### Native behavior to plan for

The functional check below verifies these on every engine it runs on.

- Removing the helper container does not remove the qdisc from a live namespace. Remove the qdisc first.
- When the target restarts, Docker gives it a new namespace (a new `SandboxID`) without the qdisc. A helper that is still running stays in the old namespace, where the target's interface no longer exists.
- The delay applies to packets that leave through the target's interface. A request and its answer cross it once each way, so a TCP request to a delayed target takes at least the delay, typically twice the delay (the SYN-ACK and the answer).

## packet-capture helper (contract 1)

The packet-capture helper records packets in the network namespace of a target container, below Ceph. A module starts the helper in that namespace (Docker network mode `container:<target>`), runs `tcpdump` there and retrieves the capture file. The target keeps its own image and capabilities. The helper contains no Ceph component, and nothing in it decodes, decrypts or judges Ceph Messenger traffic: that belongs to the consumers ([ceph-testcontainers-go#52](https://github.com/JSYoo5B/ceph-testcontainers-go/issues/52) collects and structurally validates the files).

This section defines contract 1, its quick check and its functional check, which [image/extension_check.py](../image/extension_check.py) runs. Publication comes in a later change. Until then no helper image is published, and the functional check's results are evidence for the engines it ran on, not a support claim.

### Image contract

| Requirement | Detail |
| --- | --- |
| Platform | Linux `arm64` or `amd64`. |
| User | The image's own user is not root. The module runs the helper as that user and never as root, so `tcpdump` neither drops privileges nor changes the ownership of its output. |
| Capability | `tcpdump` holds `cap_net_raw` as a permitted and effective file capability. The module grants `NET_RAW` alone (`--cap-drop=ALL --cap-add=NET_RAW`) and does not set `no-new-privileges`, which would discard the file capability. |
| Entry | The module overrides entrypoint and command; `sleep infinity` keeps an idle helper running. |
| Executables on `PATH` | `sh`, `sleep`, `tcpdump`, `python3` |
| Version | `tcpdump --version` reports a tcpdump and a libpcap version. |
| Filter | `tcpdump -y EN10MB -d <filter>` compiles a filter without a device; an invalid filter fails with `can't parse filter expression`. The module compiles every filter this way before it starts a capture. |
| Capture | `tcpdump -p -i <interface> -s <snaplen> -c <packets> --immediate-mode -U -w <file> <filter>` writes classic pcap. Rotation (`-C`, `-G`, `-W`) and pcapng are not part of contract 1. |
| Readiness | tcpdump prints `tcpdump: listening on <interface>, link-type <type> ..., snapshot length <snaplen> bytes` on standard error once its device and filter are active. The module waits for that line and for the output file before it starts the workload; that the file is open by then is for the functional check to verify. A running container or an existing file alone is not readiness. |
| Graceful stop | `SIGTERM` or `SIGINT` ends the capture with exit status 0, the file flushed and closed, and its statistics on standard error: `<n> packets captured`, `<n> packets received by filter`, `<n> packets dropped by kernel`. A statistic that tcpdump does not print, such as packets dropped by the interface, is unknown, not zero. |
| Bounds | A capture ends at `-c <packets>` or at the module's stop. The file can hold at most 24 + `<packets>` × (16 + `<snaplen>`) bytes; the module derives its byte bound from that, not from a rotation threshold. |
| Without `NET_RAW` | Because its file capability is effective, the kernel refuses to start `tcpdump` at all when `NET_RAW` is missing from the bounding set (Docker reports `exec ...: operation not permitted`). A capture is never attempted with fewer privileges. |
| Command supervision | `python3` imports `base64`, `ctypes`, `json`, `os`, `select`, `signal`, `subprocess`, `sys` and `time`, and `prctl(PR_SET_CHILD_SUBREAPER)` succeeds through `ctypes`. |
| Filesystem | `/tmp` is writable by the image's user; captures and the supervisor's markers live below it. |
| Runtime closure | Each executable loads with its libraries and data. |

The reference helper also carries the labels `io.ceph-testcontainers.extension=capture` and `io.ceph-testcontainers.extension.contract=1`. Other helpers do not need them, and the checker does not rely on them.

### Quick check

```sh
make capture-image                        # build ceph-testcontainers:extension-capture
make capture-check                        # its quick check
python3 image/extension_check.py --extension capture --image my-registry/capture-helper:dev
```

The quick probe ([check-capture.sh](../image/extensions/capture/check-capture.sh)) runs as the image's own user in a container with no network and `NET_RAW` alone. Besides the executables, version, user, file capability, filter and supervisor checks, it captures on the loopback with the options above: it waits for the readiness line, sends one UDP datagram to port 7000 and one to port 7001, each with its own payload, stops tcpdump with `SIGTERM`, and requires exit status 0, `1 packet captured`, the statistics, and a classic pcap header (version 2.4, snaplen 128, Ethernet link type). It then decodes every record's Ethernet, IPv4 and UDP headers: the file must hold exactly the datagram to port 7000 with its payload, and not the one to port 7001. The probe uses only the contract's executables and shell builtins. A separate container without any capability must then fail to start `tcpdump --version` in exactly the way the runtime refuses an executable whose file capability the bounding set lacks: the container started, exited with status 255 and printed only `exec .../tcpdump: operation not permitted`. A permission error of tcpdump itself does not pass and fails the image. A container whose process Docker or the runtime never ran (not started, a runtime state error, or a `docker start` status that differs from the container's) fails the check as `checker_failure`, which says nothing about the image. `extension-report.json` keeps every check, the tcpdump version and the cleanup.

These are checks of the image on one engine without a network. They do not show capture in another container's namespace, filter exclusion of real peer traffic or behavior on other engines; the functional check below covers the first two.

### Functional check

```sh
make capture-functional-check            # in a container of the official image
python3 image/extension_check.py --extension capture --image my-registry/capture-helper:dev \
  --functional --target quay.io/ceph/ceph:v20.2.4
```

After the quick check, each `--target` gets its own scenario on a private bridge (`docker network create --internal`) that the checker owns:

1. A container of the target image idles in `sleep infinity` with its own image and capabilities. A server from the helper image joins its namespace without any capability and answers on TCP ports 7000 (selected) and 7001 (excluded). A peer from the helper image on the bridge sends the requests.
2. The capturing helper joins the target's namespace (`--network container:<target>`) as the image's own user with `--cap-drop=ALL --cap-add=NET_RAW`, and no other option. The checker requires a non-root UID, the target's namespace, and the interface that carries the target's MAC address, found through sysfs.
3. The filter `tcp port 7000 and host <peer>` must compile with `tcpdump -y EN10MB -d`.
4. Each capture runs `tcpdump -p -i <interface> -s 128 -c <packets> --immediate-mode -U -w <file> <filter>` in the background under `sh`, which records its PID and exit status. The checker waits for the readiness line with the requested interface, `EN10MB` and snaplen, and for the output file, before any traffic, within 15 seconds. A tcpdump that exits first fails with the kind of its error.
5. The traffic capture: the peer exchanges a request and a 1000-byte answer on port 7000, then on port 7001, both verified by the peer. `SIGTERM` must end tcpdump within 15 seconds with exit status 0 and its three statistics. The file must be classic pcap (microsecond magic, version 2.4, snaplen 128, Ethernet) with whole records within the snaplen, as many as `packets captured`, and within the bound of 24 + `<packets>` × (16 + 128) bytes. Every record must be IPv4 TCP between the peer and the target's port 7000: the SYN, the SYN-ACK, the request payload and the answer's payload, with the answer cut at the snaplen. A record of port 7001 or of any other flow fails.
6. The empty capture: the same filter while the peer uses port 7001 only. It must stop the same way with `0 packets captured` and a 24-byte file, the header alone. This is a valid result, distinct from a failed start.
7. The bound capture: `-c 3` with traffic on port 7000. tcpdump must exit on its own with status 0 and exactly three records of the selected flow.
8. Negative controls in the same namespace: an invalid filter (`invalid_filter`), an interface that does not exist (`unavailable_interface`), and the helper without `NET_RAW`, which the runtime must refuse to execute as in the quick check (`permission_denied`): the container started, exited with status 255 and printed only the exec refusal. A tcpdump that starts and reports a permission error of its own fails this control, and a container whose process Docker or the runtime never ran is a `checker_failure`. Each control must fail before readiness with its own kind.
9. Every container and the network carry the scenario's label and are removed; cleanup passes only when no labelled container or network remains.

A failure records its stage (`setup`, `capture:traffic`, `capture:empty`, `capture:bound`, `refusals` or `cleanup`) and kind. A Docker command that fails, or a peer exchange that fails, is a `checker_failure`, which says nothing about the image. A root helper is `image_unsupported`. A tcpdump that fails to start is `invalid_filter`, `unavailable_interface`, `permission_denied`, `loading_failure` or `capture_failure`. A capture that starts but misses its evidence, and a negative control that does not fail as required, are `capture_failure`. A statistic that tcpdump did not print is `null` in the report, never zero. The readiness and stop waits bound every Docker call by the time left in their budget and accept readiness or an exit only when it was observed before the deadline; a Docker call that outlasts the time left is a `timeout`.

The raw capture files never enter the report directory: the checker copies each into a temporary directory, decodes it, and reports only its size, SHA-256, statistics and decoded summary (record counts per direction, the flow's markers and the records cut at the snaplen). CI uploads that directory as an artifact.

The functional check does not yet cover abrupt stops with partial output, a target that stops or restarts during a capture, byte limits other than the packet bound, IPv6 targets, or other engines such as rootless Docker or Podman.

### Reference helper

[image/extensions/capture](../image/extensions/capture) builds on the pinned `debian:bookworm-slim` base of the Debian role images with Debian's `tcpdump` and `python3`. `setcap` gives `tcpdump` its file capability during the build and is removed again; the image runs as Debian's `tcpdump` user. `/usr/share/ceph-testcontainers/SOURCES.txt` and `source-packages.txt` list every package with its source package and version. CI builds it and runs the quick and the functional check, with the official image of every supported release as targets, on native runners of both architectures; it is not published.

Debian's `tcpdump` drops root privileges to its `tcpdump` user and changes the ownership of its output file when it starts as root. With `NET_RAW` alone it then fails (`Couldn't change ownership of savefile`), and `-Z root` fails as well. Running it unprivileged with the file capability avoids both, so contract 1 asks for `NET_RAW` only, not `CHOWN`, `SETUID` or `SETGID`.

### Raw captures

A capture can hold payloads, addresses and identifiers. Text and JSON redaction do not sanitize a pcap. A module keeps capture files only where its caller selected them, and publishing checker artifacts that contain packets needs a deliberate artifact policy.

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
| Functional (`--functional`) | The quick check, then the runtime on this engine: netem added, changed and removed on the loopback of a disposable namespace with `NET_ADMIN`, the delay scenario for each `--target`, and the scenario of each `--selector` profile for each `--target` |
| Bandwidth profile (`--rate`) | Whether the image's `tc` parses netem's rate and limit, in a container with no network and no capabilities. With `--functional`, also whether this engine's kernel keeps them, and the [rate scenario](#bandwidth-profile) for each `--target` |
| Packet-fault profile (`--packet-faults`) | Whether the image's `tc` parses netem's reorder, duplicate and corrupt with correlations, in a container with no network and no capabilities. With `--functional`, also whether this engine's kernel keeps them, and the [packet-fault scenario](#packet-fault-profile) for each `--target` |

The delay scenario uses a private bridge network:

1. It starts a container of the target image, a server and a helper with `NET_ADMIN` in the target's namespace, and a client container on the same network.
2. It finds the target's interface by its address and MAC address, and records its root qdisc.
3. It times requests before the delay, adds a 200 ms netem delay (`--delay-ms`) with its own handle, and times them again. The median must reach the delay, and netem must count packets.
4. It changes the delay to half with a tenth of jitter, removes the helper, and checks from a new helper that the qdisc is unchanged.
5. It removes the qdisc, checks that the root qdisc is the original one again, and times requests once more.
6. It adds the delay again and restarts the target. The target must get a new network sandbox, the helper left in the old namespace must no longer see the target's interface, and a new helper must find the original root qdisc.
7. It removes every container and network it created. Cleanup passes only when the final inventory succeeds and is empty.

`extension-report.json` records the image identity and labels, every check, the engine (`docker info`), the kernel, each target's image ID, interface and namespace, the timings, the netem counters, and the cleanup. A failure carries a kind: `image_unsupported`, `permission_denied`, `kernel_unsupported`, `runtime_failure`, `timeout` or `checker_failure`.

### Selector profiles

A selector profile delays or drops only the packets that the target sends to selected peers, families and TCP destination ports, through an owned qdisc and filter hierarchy. Contract 1 promises these profiles: the publication gate below requires each of them, and a module may build peer-selective faults on them.

| Profile | Selects |
| --- | --- |
| `ipv4-tcp-u32` | The target's IPv4 egress to peer B, TCP, destination port 7000 |
| `ipv6-tcp-u32` | The same over IPv6, for TCP directly after the IPv6 base header |
| `multi-peer-u32` | Three leaves in one hierarchy: peer B over IPv4 on port 7000, peer C over IPv4 on port 7001, and peer B over IPv6 on port 7000 |

Every profile uses the same owned hierarchy, laid out as the module lays out a delay with several peers. A u32 filter has one protocol, so each family gets its own preference: IPv4 first, from preference 10. The kernel numbers the u32 tables of one qdisc in the order that their preferences are created, from `800:`, and each leaf names its table, so a table numbered otherwise refuses the leaf instead of taking it.

```text
root prio 7e00:, 3 bands, every priority mapped to band 1 (7e00:1)
child netem 7e10:, parent 7e00:3
IPv4 leaves: preference 10, table 800:, handles 800::1, 800::2, ... -> 7e00:3
IPv6 leaves: the next preference and table (11 and 801: after IPv4 leaves, else 10 and 800:) -> 7e00:3
```

```sh
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev \
  --functional --target quay.io/ceph/ceph:v20.2.4 \
  --selector ipv4-tcp-u32 --selector ipv6-tcp-u32 --selector multi-peer-u32
```

`make extension-selectors` lists the profiles. The scenario uses a private dual-stack bridge network with the target, three peers B, C and D that answer on ports 7000 and 7001 over both families, a helper with `NET_ADMIN` and a request driver in the target's namespace:

1. It finds the target's interface by its address and MAC, records its root qdisc, and times each selected path.
2. It adds the root and the child only, and times each selected path again. Until a leaf exists, every packet takes the unshaped band: the paths must stay fast and the child must count no packet, so an apply that stops before its leaves delays nothing.
3. It adds the leaves in the layout's order with a delay (`--delay-ms`) and reads the filters back. tc prints one `match` member per u32 key, repeated in one object, so the checker decodes them in order instead of as a dictionary. It compares every leaf's preference, protocol, handle, flow ID and every key's offset, value, mask and offmask with the requested one, and each preference's table with the layout. tc packs keys into words: the IPv6 next header and port appear at offsets 4 and 40. Any other filter entry is foreign state.
4. It times the controls before any selected traffic: every other combination of peer, family and port, and a reverse connection of each family from peer B to the target. They must stay fast, and the child must count no packet. Then every selected path must take at least the delay, and the child must count its packets.
5. It waits, within 2 s, until the child holds no packet (backlog and queue length 0): a completed read does not show that the queue drained. The 2 s is one deadline: each `tc` read may use only what remains of it, and an empty queue read after the deadline does not count. A read that times out, or a queue that is still busy or unobserved at the deadline, fails the scenario with `drain_pending` and its reason, and the child never changes to loss. Otherwise the checker then changes the child to 100% loss. A fresh connection on every selected path must time out, the controls must still answer, and the child must count drops.
6. It adds a filter of another owner under the root (preference 20). The readback must refuse the hierarchy as foreign state, and accept it again once that filter is removed.
7. It removes the helper and starts another in the target's namespace. Removing a helper does not remove kernel state: the new helper must find every leaf and the child with its loss.
8. It removes the leaves one by one in reverse order, then any preference whose u32 table outlived its leaves (removing the last leaf can remove its whole chain), then the root. The root qdisc must be the original one again, no filter may remain, and every selected path must be fast again.
9. It adds the whole hierarchy again and restarts the target. The target must get a new network sandbox, the helper left in the old namespace must no longer see the target's interface, and a new helper must find the original root qdisc and no filter.

`extension-report.json` keeps each profile's result under `selectors`, and each scenario directory keeps the raw filter dump (`filter.json`) and every timing. The `ipv6-tcp-u32` leaves do not select IPv6 traffic with extension headers or fragments, and no profile covers IPv4 options, IPv4-mapped addresses or receiver-side shaping. A module shapes the other direction of a path at the peer's egress instead.

The selector profiles also need the engine's kernel to provide the prio qdisc and the u32 classifier (see [Runtime requirements](#runtime-requirements)). On an arm64 Docker Desktop engine, the `ipv4-tcp-u32` and `ipv6-tcp-u32` scenarios of an earlier checker version passed against the official image and the OSD role image of 20.2.4 and 19.2.5, with the published `extension-netem-v1` helper. With a 200 ms delay, the selected path took 410 to 412 ms and every control 0.06 to 0.11 ms; the child drained within 170 ms.

### Bandwidth profile

The bandwidth profile limits the packets that a selector hierarchy selects to a rate. It uses netem's own rate in the owned child, so no other qdisc and no other tool is involved. A module may offer a rate only after this profile passed for the helper and the engine.

- The rate is in bits per second, as `tc` takes it (`rate 16000000bit`). It must be whole bytes per second, a multiple of 8 bit/s. `tc -j qdisc show` prints it in bytes per second, under `options.rate.rate` (`2000000` for 16 Mbit/s).
- netem sends a packet only after the previous one has taken its length over the rate, so there is no burst. A packet counts with its Ethernet header, as the kernel queues it, and a segmentation offload packet counts with all of its segments. An application therefore gets less than the rate: about 95% of it for a bulk TCP transfer.
- The packets that wait for the rate, and for a delay, share one queue of `limit` packets. A packet that finds the queue full is dropped and counted in the child's `drops`. `tc` uses 1000 packets when the command gives no limit; modules pass the limit explicitly.
- A rate composes with a delay and a loss in the same netem qdisc (`netem delay 50ms rate 16000000bit limit 1000`). netem keeps an earlier rate when a change gives none, so a module states the rate in every change and removes the qdisc to end it.
- Every selected peer, family and port of one target shares the one rate: two selected transfers at once each get about half. A rate for each peer needs its own hierarchy, which contract 1 does not describe.
- Like every selector profile, the rate shapes only what the target sends. A module limits the other direction of a path at the peer's egress.

```sh
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev --rate
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev \
  --functional --target quay.io/ceph/ceph:v20.2.4 --rate --rate-kbit 16000
```

The checker keeps the outcome of each stage under `rate` in `extension-report.json`, with the stage that failed:

1. Parser: in a container with no network and no capabilities, `tc qdisc add dev lo root handle c7e3: netem rate 1000000bit limit 100` must fail with `Operation not permitted`. Any earlier parse error is `image_unsupported`. This stage depends on the image alone; a quick check with `--rate` runs only this stage.
2. Kernel: in a disposable namespace with `NET_ADMIN` and no network, the checker adds the same qdisc to the loopback and reads `tc -j` back: 125000 bytes/s and a limit of 100. It changes the qdisc to add a 10 ms delay, reads all three back, and removes its handle; the loopback must have its original root qdisc again. A kernel that does not keep the rate or the limit is `kernel_unsupported`.
3. Targets: the rate scenario below, for each `--target`.

A failed stage skips the later ones. The baseline contract, the delay scenario and the selector profiles still run and are reported, so a report shows when a helper is valid for delays but not for rates. Nothing is installed when the rate is missing, and no other shaper replaces netem's rate.

The rate scenario uses a private dual-stack bridge network with the target, peer B and peer C, which receive TCP transfers on port 7000 and answer each with its byte count, its SHA-256 and the address that it came from. A sender in the target's namespace sends a transfer of two seconds at the rate (4 MB at the default 16 Mbit/s), so the data leaves through the target's interface:

1. It finds the target's interface, records its root qdisc, and sends a baseline transfer on every path: peer B and peer C, over IPv4 and over IPv6.
2. It adds the owned hierarchy with a netem child of `rate <rate>bit limit 1000` and two leaves: peer B over IPv4 and peer B over IPv6, port 7000. It reads the leaves back as the selector profiles do, and reads the child's rate, limit and delay from `tc -j`.
3. It sends to peer C first, over both families. These transfers must stay at least four times the rate, and the child must count no packet.
4. It sends to peer B over each family in turn, then over both at once. Each transfer must reach 50% to 105% of the rate, and the two at once must share it: together 50% to 105% of the rate.
5. It changes the child to a 50 ms delay with the same rate and limit. The connection must take at least the delay, and the transfer must reach 25% to 105% of the rate.
6. It changes the child to a queue of 5 packets. The transfer must stay under 105% of the rate, and the child must count new drops.
7. It removes the leaves and the root, then the helper. The root qdisc must be the original one, no filter may remain, the helper must be gone, and a new helper without `NET_ADMIN` must find the original root qdisc and no filter. Transfers to peer B over both families must be fast again.

Every transfer must carry exactly its bytes with the same SHA-256, and must use the family of its path: the sender's socket family, and the address that the peer saw, which must be the target's own address of that family. The report keeps the configured rate, limit, delay and the child's counters of each phase apart from the throughput that the transfers measured, and each scenario directory keeps the filter dump and every transfer.

The bandwidth bounds are wide on purpose: they tell a rate apart from no rate and from a rate that is not shared, not the precision of netem. On a native Linux Docker Engine (amd64), the published `extension-netem-v1` helper passed every stage with 16 Mbit/s against the official images and the OSD role images of 20.2.4 and 19.2.5. Each selected transfer reached 1.89 to 1.91 MB/s (94% to 96% of the rate), two at once 1.90 MB/s together, the unselected peer and the restored paths stayed near 1 GB/s, a 50 ms delay with the rate took 1.74 MB/s, and a queue of 5 packets dropped 36 to 48 packets while TCP still delivered every byte.

### Packet-fault profile

The packet-fault profile reorders, duplicates or corrupts a percentage of the packets that a selector hierarchy selects. It uses netem's own faults in the owned child, so no other qdisc and no other tool is involved. A module may offer these faults only after this profile passed for the helper and the engine. The checker selects it only with `--packet-faults`; it does not change contract 1, the selector profiles or the bandwidth profile.

- Each fault is a percentage of the selected packets with a correlation: `reorder 25% 50%`, `duplicate 3% 10%`, `corrupt 2% 5%`. The correlation, from 0 to 100%, makes each packet's random decision depend on the previous one. `tc -j qdisc show` prints each fault as an object with its probability under the fault's own name and its correlation, both as fractions (`"reorder":{"reorder":0.25,"correlation":0.5}`), and prints no member without the fault.
- A reorder sends that percentage of the packets at once, ahead of the packets that wait for the delay, so it needs a delay. tc sets the reorder gap to 1 (every packet may be reordered) and prints it as `"gap":1`.
- A duplicate sends a second copy of that percentage of the packets. tc (iproute2 6.1, as in the published helper) drops a duplicate's correlation silently unless a delay comes first: `netem duplicate 3% 10%` installs a correlation of 0. Modules therefore always send a delay with its jitter and a correlation of 0% before a duplicate, which is `delay 0us 0us 0% duplicate 3% 10%` without a delay. The kernel stage checks this exact encoding and records what the plain one installs.
- A corrupt flips one random bit in the linear part of that percentage of the packets, after netem has computed a checksum that the sender left to the device, and splits a segmentation offload packet to corrupt one segment. The receiver drops a corrupted segment when its checksum fails, and TCP sends it again. A bit in an Ethernet or IPv6 header, or an IPv4 header that the Docker bridge checks first, is lost before the receiver counts it.
- Duplicate and corrupt compose with each other, a delay, a loss and a rate in the same netem qdisc. The order of the options is the Go module's: delay, loss, the faults, then rate and limit.
- Like every selector profile, the faults affect only what the target sends. A module affects the other direction of a path at the peer's egress.

These compositions are not offered. The checker records them under `unsupported` in its report and never installs them on a target, and a module refuses them before it changes anything:

| Composition | Reason |
| --- | --- |
| Reorder without a delay | netem reorders by sending some packets ahead of the delayed ones. tc refuses it (`reordering not possible without specifying some delay`); the parser stage records that outcome. |
| Reorder with jitter | A jitter reorders packets by itself, and the two cannot be told apart. |
| Reorder with a rate | netem sends the reordered packets past the rate limit. |

```sh
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev --packet-faults
python3 image/extension_check.py --extension netem --image my-registry/netem-helper:dev \
  --functional --target quay.io/ceph/ceph:v20.2.4 --packet-faults
```

The checker keeps the outcome of each stage under `packet_faults` in `extension-report.json`, with the stage that failed:

1. Parser: in a container with no network and no capabilities, `tc qdisc add dev lo root handle c7e4: netem delay 10000us 0us 0% reorder 25% 50% duplicate 3% 10% corrupt 2% 5%` and `... netem delay 0us 0us 0% duplicate 3% 10%` must each fail with `Operation not permitted`. Any earlier parse error is `image_unsupported`. The stage also records what tc does with a reorder without a delay. This stage depends on the image alone; a quick check with `--packet-faults` runs only this stage.
2. Kernel: in a disposable namespace with `NET_ADMIN` and no network, the checker adds each of the two qdiscs to the loopback, reads every probability, correlation, the gap and the delay back from `tc -j`, and removes its handle; the loopback must have its original root qdisc again. The first must read back reorder 25% with 50%, duplicate 3% with 10%, corrupt 2% with 5%, gap 1 and a 10 ms delay; the second duplicate 3% with 10%, gap 0 and no delay. A missing fault, a missing or different correlation and a gap other than 1 with a reorder are `kernel_unsupported`. It then installs `netem duplicate 3% 10%` and records the correlation that it kept, without relying on it.
3. Targets: the packet-fault scenario below, for each `--target`.

A failed stage skips the later ones. The baseline contract, the delay scenario, the selector profiles and the bandwidth profile still run and are reported, so a report shows when a helper is valid for delays and rates but not for packet faults. Nothing is installed when a fault is missing, and nothing replaces netem's faults.

The packet-fault scenario uses a private dual-stack bridge network with the target, peer B and peer C, which receive TCP transfers on port 7000 and answer each with its byte count, its SHA-256 and the address that it came from. A sender in the target's namespace sends 8 MB per transfer, one transfer at a time, and the checker reads the receiving peer's kernel counters (`/proc/net/netstat` and `/proc/net/snmp` of its namespace) before and after each transfer:

| Effect | netem child | Receiver counter |
| --- | --- | --- |
| reorder | `delay 10000us reorder 25% 25%` | `TcpExt.TCPOFOQueue`: segments that arrived ahead of a gap |
| duplicate | `delay 0us 0us 0% duplicate 50% 10%` | `TcpExt.TCPDSACKOldSent` + `TCPDSACKOfoSent`: duplicates acknowledged with a D-SACK |
| corrupt | `corrupt 20% 10%` | `Tcp.InCsumErrors` + `IpExt.InCsumErrors`: segments that failed their checksum |
| composed | `delay 5000us 0us 0% duplicate 50% 10% corrupt 20% 10% rate 100000000bit limit 1000` | the duplicate and corrupt counters, each on its own |

1. It finds the target's interface and records its root qdisc, and the checksum and offload settings of the sender's and peer B's interfaces (through the ethtool ioctls, for context only). It sends a baseline transfer on every path: peer B and peer C over IPv4 and over IPv6. The most that a baseline transfer raised a counter is that counter's noise.
2. For each effect in turn, it adds the owned hierarchy with that netem child and two leaves (peer B over IPv4 and over IPv6, port 7000), reads the leaves back as the selector profiles do, and reads the child's faults, correlations, gap, delay and rate from `tc -j`. They must be the requested ones, with gap 1 for the reorder and 0 otherwise.
3. It sends to peer C over both families first. The child must count no packet, and the effect's counter may rise by at most its noise plus 10.
4. It sends to peer B over each family. The child must count packets, and each transfer must raise the effect's counter by at least 20 and by at least its noise plus 15. Natively each transfer raised it by 113 or more, and a transfer without the fault by at most 5.
5. It removes the leaves and the root. The root qdisc must be the original one and no filter may remain, before the next effect.
6. After the last effect, it removes the helper. The helper must be gone, and a new helper without `NET_ADMIN` must find the original root qdisc and no filter. Transfers to peer B over both families must raise no effect counter by more than its noise plus 10.

Every transfer, with or without a fault, must carry exactly its bytes with the same SHA-256, and must use the family of its path and come from the target's own address of that family. The report keeps four things apart for each effect: the requested parameters, the state that tc reports, the receiver's counters (each raw counter and each effect counter), and the transfers. It also keeps the helper and target image IDs, the engine and kernel, and the offload settings. The bounds tell a fault apart from no fault and from a fault on the wrong path, not netem's precision: random events are never compared with an exact count.

The receiver's counters show that the selected packets were reordered, duplicated or corrupted on the wire and that TCP recovered. They do not show that a changed Ceph Messenger frame reached Ceph's parser.

On a native Linux Docker Engine (amd64, checksum, scatter-gather, TSO and GSO on and GRO off on both ends), the published `extension-netem-v1` helper passed every stage against the official images and the OSD role images of 20.2.4 and 19.2.5. In two runs, every selected transfer of 8 MB raised its effect's counter by 113 or more over IPv4 and IPv6: reorder 241 to 315, duplicate 113 to 140, corrupt 332 to 987, and composed 1796 to 1969 D-SACKs with 1039 to 1269 checksum errors. Baseline, unselected and restored transfers raised a counter by at most 5, and every transfer kept its exact bytes and SHA-256. tc reported every requested probability and correlation, gap 1 with the reorder, and kept the duplicate's correlation only with the delay encoding: `netem duplicate 3% 10%` installed a correlation of 0. A test image whose `tc` rejects `corrupt` (`What is "corrupt"?`) failed the profile at the parser stage with `image_unsupported`, while its baseline delay scenario and bandwidth profile still passed.

## Published helper

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-netem-v1
```

The tag lists `linux/amd64` and `linux/arm64`; `extension-netem-v1-linux-<architecture>` selects one platform. The contract version is part of the tag, so a later incompatible contract gets a new tag.

- Built from [image/extensions/netem](../image/extensions/netem) on the pinned `debian:bookworm-slim` base of the Debian role images, with Debian's `iproute2` and `python3` (iproute2 6.1.0 and Python 3.11.2 when this was written). The `linux/arm64` image is 45 MB compressed and 155 MB unpacked.
- `/usr/share/ceph-testcontainers/SOURCES.txt` and `source-packages.txt` list every package with its source package and version.
- CI checks the helper functionally on native runners of both architectures, with every selector profile, the bandwidth profile, the packet-fault profile, and the official image and the published OSD role image of every release in [image/releases.py](../image/releases.py) as targets. Publication requires every target, every selector profile, the bandwidth profile and the packet-fault profile to pass and clean up on both architectures. A manual run with `publish` uploads the exact tested images, promotes the tag only when both architectures passed in the same run, and then checks the published digests again the same way.
- The helper is checked and published apart from the release images. Its failure never blocks them, and theirs never blocks it.
- The weekly published image health workflow also runs `python3 image/publish.py extension-health`, which fails when the helper tag no longer lists both platform images.

### Engine coverage

| Engine | Platforms | Evidence |
| --- | --- | --- |
| Docker Engine on GitHub-hosted Ubuntu 24.04 runners | `amd64`, `arm64` | Functional check with every selector profile, the bandwidth profile and the packet-fault profile before every publication and on the published digests |
| Native Linux Docker Engine | `amd64` | Functional check with every selector profile, the bandwidth profile and the packet-fault profile, with the official 20.2.4 and 19.2.5 images and their published OSD role images |
| Docker Desktop | `arm64` | Functional check with the official 20.2.4 and 19.2.5 images and their published OSD role images, and the single-family selector profiles of an earlier checker version; the bandwidth and packet-fault profiles are not verified |

Other engines, rootless Docker, Podman and Docker Desktop's Enhanced Container Isolation are not covered. Run the functional check on them before relying on the helper.

### Why a helper and not extended role images

The other way to provide `tc` is to add it to the role images and run `tc` inside each daemon container.

- Every role image of every variant and release would need the tools and a separate netem tag, and an extended `all` image would have to stay the union of the extended roles.
- Every daemon container that could be a target would need `NET_ADMIN`, and the module would have to grant it to every initial and later MON, MGR, OSD, RGW, MDS, mirror and client container.
- The official image and custom Ceph images could never be targets.

The helper avoids all three, at the cost of one more small image and one more container per delayed target. No case seen so far needs the role-image route.

## Not in contract 1

- Loss of only some of the packets, other loss models and netem's `slot`. They use the same `tc`, but only a delay, 100% loss, the bandwidth profile's rate and the packet-fault profile's faults of selected packets are checked.
- A reorder gap other than 1, and a reorder with jitter or a rate.
- A separate rate for each selected peer, and shaping with another qdisc such as `tbf` or `htb`.
- Receiver-side delay through IFB. Selectors also do not cover IPv6 extension headers or fragments, IPv4 options or IPv4-mapped addresses.
- `ss` and `nft` for socket destruction and peer-specific filtering. Each becomes its own contract with its own checks; packet capture has its own [contract](#packet-capture-helper-contract-1).
