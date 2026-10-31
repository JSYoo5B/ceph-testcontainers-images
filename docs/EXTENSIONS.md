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

Contract 1 covers delay and jitter on the egress of the target's interface on a private bridge network, and, through its [selector profiles](#selector-profiles), delay and loss of only the packets to selected peers, families and TCP ports, of only a server's replies by their TCP source port, or of only TCP connection setup. Its optional [bandwidth profile](#bandwidth-profile) adds netem's rate and queue limit to those selected packets, and its optional [packet-fault profile](#packet-fault-profile) adds netem's reorder, duplicate and corrupt. Each optional profile is selected and checked on its own. A helper that meets contract 1 without them stays valid for callers that only delay or drop packets, and a helper with the bandwidth profile alone stays valid for callers that only add a rate.

### Image contract

| Requirement | Detail |
| --- | --- |
| Platform | Linux `arm64` or `amd64`. The container runs as root. |
| Entry | The module overrides entrypoint and command; `sleep infinity` keeps an idle helper running. |
| Executables on `PATH` | `sh`, `sleep`, `tc`, `ip`, `python3` |
| Versions | `tc -V` and `ip -V` report an iproute2 version. |
| JSON queries | `tc -j -s qdisc show`, `tc -j -s filter show dev <interface>`, `ip -j link show`, `ip -j address show` and `ip -j route show` print valid JSON. |
| netem | `tc` parses `qdisc add ... netem delay <time> <jitter>`. Without `NET_ADMIN`, the kernel refuses the request with `Operation not permitted`; `tc` must not fail earlier because it lacks netem. |
| Selectors | `tc` parses `qdisc add ... prio bands 3 priomap ...` and `filter add ... u32` with `match ip` and `match ip6` keys for `dst`, `protocol`, `dport` and `sport`, and a `match u8 <value> <mask> at <offset>` key. Without `NET_ADMIN`, the kernel refuses each with `Operation not permitted`. |
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

For an offline join of publication reports, candidates, promoted digests and
published-image checks, see [Capture publication qualification](CAPTURE_QUALIFICATION.md).

The packet-capture helper records packets in the network namespace of a target container, below Ceph. A module starts the helper in that namespace (Docker network mode `container:<target>`), runs `tcpdump` there and retrieves the capture file. The target keeps its own image and capabilities. The helper contains no Ceph component, and nothing in it decodes, decrypts or judges Ceph Messenger traffic: that belongs to the consumers ([ceph-testcontainers-go#52](https://github.com/JSYoo5B/ceph-testcontainers-go/issues/52) collects and structurally validates the files).

This section defines contract 1, its quick check and its functional check, which [image/extension_check.py](../image/extension_check.py) runs. The reference helper is published as `extension-capture-v1` (see [Published helpers](#packet-capture-helper)). The functional check's results are evidence for the engines it ran on, not a support claim for others.

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
8. The abrupt capture: traffic on port 7000, then `SIGKILL` to tcpdump. Its status must be 137 and it must print no statistics, which the report keeps as `null`. The file is partial evidence: a valid header, at least one complete record, only records of the selected flow, and at most one final record that was cut off, reported as `cut_tail`.
9. Negative controls in the same namespace: an invalid filter (`invalid_filter`), an interface that does not exist (`unavailable_interface`), and the helper without `NET_RAW`, which the runtime must refuse to execute as in the quick check (`permission_denied`): the container started, exited with status 255 and printed only the exec refusal. A tcpdump that starts and reports a permission error of its own fails this control, and a container whose process Docker or the runtime never ran is a `checker_failure`. Each control must fail before readiness with its own kind.
10. The target-loss capture, last because it ends the target: traffic on port 7000, then `docker kill` of the target container while tcpdump runs. The target's interface must then be gone from the namespace that the helper joined. tcpdump either exits with a nonzero status (`end: exited`; a status 0 fails) or keeps running and is stopped with `SIGTERM` (`end: kept running`); the report records which. Its file is partial evidence as in the abrupt capture: complete records of the traffic before the loss, none of another flow. The helper never follows the target.
11. A second private bridge, dual-stack (`docker network create --internal --ipv6`), gets its own target, server, capturing helper and peer, set up and checked as in steps 1 and 2. Both the target and the peer must have an IPv4 and an IPv6 address, and the server must answer on both. The filters of steps 12 and 13 must compile.
12. The IPv6 capture: the filter `ip6 and tcp port 7000 and host <peer's IPv6 address>`. The peer exchanges a request and its answer over IPv6 on port 7000 and on port 7001, then over IPv4 on port 7000. tcpdump stops with `SIGTERM` as in the traffic capture, and the file must meet the same requirements, with every record Ethernet/IPv6/TCP between the peer's and the target's IPv6 addresses on port 7000 (TCP directly after the IPv6 base header). An IPv4 record, a record of port 7001 or of any other flow, or an IPv6 record with an extension header fails. The report counts the IPv4 records, which must be 0.
13. The restart capture, last on the dual-stack bridge because it moves its target to a new namespace: the filter `tcp port 7000 and host <peer's IPv4 address>`. The peer connects from source port 24000, then the target container is restarted (`docker restart -t 0`) while tcpdump runs. The target must run again in a new network sandbox. The checker records whether tcpdump still ran at that point, starts a new server in the new namespace and has the peer connect from source port 24001 to the target's address after the restart. The helper must not be in the new namespace, and neither the old nor the new interface of the target may be in the namespace it joined. tcpdump either exits with a nonzero status (`end: exited`) or keeps running and is stopped with `SIGTERM` (`end: kept running`); the report records which. Its file is partial evidence as in the abrupt capture, and every record must belong to the connection from port 24000. Any record of the connection from port 24001, the one after the restart, fails, and so does a record of any other connection.
14. Every container and both networks carry the scenario's label and are removed; cleanup passes only when no labelled container or network remains.

The first bridge stays IPv4 only, so steps 1 to 10 run as before the dual-stack bridge existed.

Between steps 12 and 13, on the dual-stack bridge, the scenario also records a generator-admission observation under `ipv6_extension_probe`. It is not a requirement: it has `gating: false`, adds no capture to the set that publication requires, and cannot fail the scenario. It prepares a later IPv6 extension-header capture, whose sender would have to set one eight-byte Destination Options header (next header TCP, length 0, a PadN option of four zero bytes) with `IPV6_DSTOPTS`. In the Linux reference inspected (`ipv6_set_opt_hdr` in `net/ipv6/ipv6_sockglue.c`), that option needs `CAP_NET_RAW` in the network namespace's user namespace and is checked before the header is validated, so the peer, which runs with `--cap-drop=ALL`, is expected to be refused. The kernel pin is research evidence, not the runner's identity.

Three generators each open a TCP socket, set the header, read it back with `getsockopt`, and only after a successful set connect to the target's IPv6 address on port 7000, send the request and read the 1000-byte answer:

- `capability_free`: the scenario's own peer. Its actual `errno` is recorded; `EPERM` with no effective `CAP_NET_RAW` is `refused`.
- `net_raw_user`: a new container of the helper image as its own non-root user with `--cap-drop=ALL --cap-add=NET_RAW`. The report keeps the process's effective, permitted and bounding capability sets, because a capability in a container's bounding set is not necessarily effective for a non-root process.
- `net_raw_root`: a new container with `--user=0:0 --cap-drop=ALL --cap-add=NET_RAW`.

The outcome of each is `refused`, `set_failed` (another error), `readback_mismatch` (the bytes read back are not exactly the proposed header), `exchange_incomplete`, `admitted` (exact readback and the whole answer), or `inconclusive`. A missing, null, wrong-typed or unknown field, output that is not exactly one JSON record (noise before or after it, or a second record, included), and a record that contradicts itself (a refusal or success against the effective `CAP_NET_RAW` the record itself reports, a completed exchange of another size, a readback after a refused set) are `inconclusive` and carry no evidence. A capability-free sender that reports an effective `CAP_NET_RAW` is also `inconclusive`. The probe's `summary` states whether the capability-free refusal is established and which generators were admitted, and is `null` unless all three records are conclusive. A Docker or probe failure leaves the observation `inconclusive` with the error.

The probe establishes the permission model only. The extension-header capture that follows it uses the `net_raw_root` generator; a `net_raw_root` record that is not `admitted` fails that capture (stage `capture:ipv6_ext`), while the probe's own record stays an observation.

The extension-header capture (`ipv6_ext`), right after the probe, is required like every other capture. Only the client sets the header, the profile that the admitted probe already showed: the generator's socket carries it on every packet that it sends, and the listener in the target stays capability-free, so its replies are plain IPv6. The Ceph target, the server and the capturer get no capability for it. The filter is

```text
ip6 and host <generator> and (tcp port 7000 or (ip6[6] = 60 and ip6[40] = 6 and ip6[41] = 0 and ip6[4:2] >= 28 and (ip6[48:2] = 7000 or ip6[50:2] = 7000)))
```

libpcap's `tcp port` reads a TCP header only directly after the IPv6 base header (`gen_portop6` checks the base next header), so the second branch reads this one layout at fixed offsets: next header 60 (Destination Options), then TCP with a header length of 0 (eight bytes), and a payload length that covers that header and a TCP header. The generator exchanges a request and its answer on port 7000 and on port 7001, both with the header, and the capability-free peer exchanges plainly on port 7000 from another address. The decoder accepts exactly one Destination Options header before TCP whose declared length the frame holds; a cut or overlong options header, or anything but TCP after it, is not TCP. The file must hold the whole selected flow (SYN, SYN-ACK, request and answer) and nothing else, the generator's SYN and request must carry the header, every header must be exactly the requested `0600010400000000`, and no reply may carry one. Plain traffic alone never passes, and the plain IPv6 capture does not accept a header-bearing frame. On an arm64 Docker Desktop engine with the published `extension-capture-v1` helper and the official 20.2.4 image, all five of the generator's packets carried the header, the five replies did not, and neither the excluded port nor the peer was captured.

A failure records its stage (`setup`, `capture:traffic`, `capture:empty`, `capture:bound`, `capture:abrupt`, `refusals`, `capture:target_loss`, `setup:dual_stack`, `capture:ipv6`, `probe:ipv6_extension_generator`, `capture:ipv6_ext`, `capture:restart` or `cleanup`) and kind. A dual-stack bridge that gives the target or the peer no IPv6 address, and a restarted target without a new network sandbox, are a `checker_failure`. A Docker command that fails, or a peer exchange that fails, is a `checker_failure`, which says nothing about the image. A root helper is `image_unsupported`. A tcpdump that fails to start is `invalid_filter`, `unavailable_interface`, `permission_denied`, `loading_failure` or `capture_failure`. A capture that starts but misses its evidence, and a negative control that does not fail as required, are `capture_failure`. A statistic that tcpdump did not print is `null` in the report, never zero. The readiness and stop waits bound every Docker call by the time left in their budget and accept readiness or an exit only when it was observed before the deadline; a Docker call that outlasts the time left is a `timeout`.

The raw capture files never enter the report directory: the checker copies each into a temporary directory, decodes it, and reports only its size, SHA-256, statistics and decoded summary (record counts per direction, the flow's markers and the records cut at the snaplen). CI uploads that directory as an artifact.

The functional check does not yet cover byte limits other than the packet bound, IPv6 extension headers other than one eight-byte Destination Options header on the client's packets (hop-by-hop options, routing, fragments, several headers, or headers on the replies), or other engines such as rootless Docker or Podman. A partial capture's report is evidence of what was captured before the end, never of a complete capture.

### Reference helper

[image/extensions/capture](../image/extensions/capture) builds on the pinned `debian:bookworm-slim` base of the Debian role images with Debian's `tcpdump` and `python3`. `setcap` gives `tcpdump` its file capability during the build and is removed again; the image runs as Debian's `tcpdump` user. `/usr/share/ceph-testcontainers/SOURCES.txt` and `source-packages.txt` list every package with its source package and version. CI builds it and runs the quick and the functional check, with the official image of every supported release as targets, on native runners of both architectures, and publishes exactly the image that passed (see [Published helpers](#packet-capture-helper)).

Debian's `tcpdump` drops root privileges to its `tcpdump` user and changes the ownership of its output file when it starts as root. With `NET_RAW` alone it then fails (`Couldn't change ownership of savefile`), and `-Z root` fails as well. Running it unprivileged with the file capability avoids both, so contract 1 asks for `NET_RAW` only, not `CHOWN`, `SETUID` or `SETGID`.

### Raw captures

A capture can hold payloads, addresses and identifiers. Text and JSON redaction do not sanitize a pcap. A module keeps capture files only where its caller selected them, and publishing checker artifacts that contain packets needs a deliberate artifact policy.

This repository has no such policy, so CI never uploads a raw capture. The checker keeps capture files out of its report directory. Before CI uploads a capture report directory, `python3 image/publish.py check-artifacts --directory <dir>` fails when any file below it is named `*.pcap*` or starts with a classic pcap or pcapng magic number, and staging a capture helper for publication refuses a report directory that holds one. The report upload runs only when that check succeeded (`steps.raw-captures.outcome == 'success'`), so a failed check leaves the directory unuploaded, while a safe report is still uploaded after an earlier checker failure.

### Capture end-state experiments

`--capture-experiments` (with `--functional` for capture, as CI runs it) adds observations of two end states that the checker accepts but that the ordinary captures do not produce: a cut final record and a tcpdump that keeps running after its target's loss. They are recorded under `experiments` in each target's result with `gating: false`, never fail the scenario and are not part of what publication requires. Each branch is its own outcome: no result of one stands in for another, nor for the abrupt, target-loss or restart captures. The "does not cover" list below changes only for a branch observed natively on both architectures.

#### File-size limit

[image/capture_experiment_supervisor.py](../image/capture_experiment_supervisor.py) runs one tcpdump under a process-local `RLIMIT_FSIZE` that is set only after a verified complete-record prefix exists, and [image/capture_experiment.py](../image/capture_experiment.py) judges its receipt with the file. The checker drives it on the first bridge, after the ordinary captures and before the negative controls, once for each cut:

| Experiment | Extra bytes past the prefix | Child's `SIGXFSZ` |
| --- | --- | --- |
| `header` | 8, inside the next 16-byte record header | `default` |
| `body` | 36, inside the next record's data | `ignore` |

The host side (`SupervisorChannel`) is one `docker exec -i` into the capturer: commands go to its standard input as JSON lines, and each event comes back as one JSON line. A line counts only once its newline arrived, every read is bounded in time and length, and a disconnect, a malformed line or a line that is not an object is never acted on. The order is fixed: the supervisor reports tcpdump's readiness; the peer's seed exchange on port 7000 fills the file; the checker reads the file back once it stopped growing for a second and requires a whole number of complete records; only then is the limit sent, once, for exactly that prefix. Later traffic (two more exchanges) is released only by an acknowledgement that `limit_problems` accepts: the limit applied, read back on the child, the supervisor's own limit unchanged, the file stable at the prefix and the requested `SIGXFSZ` disposition. A refused, mismatched, missing or ambiguous acknowledgement ends the experiment as `inconclusive` without later traffic and without a second limit command. The supervisor then waits for tcpdump to end, sends `SIGTERM` when it still runs, and its receipt and the file go to the oracle. The `docker exec` is ended on every path.

On an arm64 Docker Desktop engine with the published `extension-capture-v1` helper and the official 20.2.4 image, both were `observed`. With `header`, a prefix of 943 bytes (ten records) and a limit of 951, tcpdump was killed by `SIGXFSZ` and the file ended in a cut record header of 8 bytes. With `body`, the limit of 979 bytes made the write fail with `EFBIG` while tcpdump kept running; `SIGTERM` ended it with status 0, and the file ended inside a record's data.

The functional check's capture wrapper redirects tcpdump's standard error to a regular file. Linux applies the writing task's `RLIMIT_FSIZE` to every regular file it writes, so a small limit set on tcpdump would also cut its diagnostic stream. The supervisor therefore owns the stream:

- tcpdump's standard error is a pipe read by the supervisor, whose own limit is never changed (it is read before and after the child's limit is lowered). Retention is bounded to a head and a tail; anything dropped is counted, and an overflow makes the result inconclusive, with statistics reported as unknown rather than missing.
- The child's `SIGXFSZ` disposition is always named by the launcher (`default` or `ignore`), recorded with the actual disposition read from `/proc/<pid>/status` after the limit is set, and the two must agree. CPython ignores `SIGXFSZ` at initialization and `subprocess` restores it only with `restore_signals`, so leaving it implicit would silently choose between a child killed by the signal and a write that fails with `EFBIG`.
- The limit is lowered with `resource.prlimit` on the child alone, only while the pcap is exactly the prefix length, then read back. The module is not promised by contract 1, so its absence, a refusal or an error is a recorded result and never a pass.

The oracle reports how tcpdump ended (`termination`) and the file's end (`cut_record`: `header`, `body` or `none`) as separate outcomes. A `SIGKILL`, whether sent by the supervisor or not, is never relabelled as the `SIGXFSZ` branch or as the abrupt capture's success, a child that outlives the window is reaped and reported as `still_running`, and a missing, null or wrong-typed field, an unstable or altered prefix, a prefix that is not a whole number of records, a file over its limit or a limit that was not read back stays inconclusive.

#### Kept running after the target's loss

`kept_running_loopback` is a diagnostic cohort of the loopback, on a bridge of its own, never a result for the target's own interface. Linux creates one loopback per network namespace and frees the namespace only at its last reference, and libpcap reports the disappearance of its own selected device. So a helper that keeps the target's namespace may keep `lo` after Docker removed the target's veth. tcpdump captures on `lo` in the target's namespace with the filter `tcp port 7000 and host 127.0.0.1`, the capturer exchanges with the server over `127.0.0.1`, and the target container is killed. Over three samples one second apart, tcpdump must still run and not be stopped (its `State` in `/proc` is running or sleeping, so a `SIGSTOP` never passes), the namespace must be the one from before, the target's interface must be gone and `lo` must be up. `SIGTERM` must then end tcpdump with status 0, its statistics and a file of the whole loopback flow. The outcome is `kept_running`, `exited` (tcpdump ended by itself) or `inconclusive` with the reasons.

On the same arm64 Docker Desktop run, the outcome was `kept_running`: in every sample tcpdump was sleeping, `lo` was up, the namespace was unchanged and the veth was gone, and `SIGTERM` ended it with status 0 and the whole flow.

#### Design references

The reference sources behind the design (research references, not the identity of any helper): [Linux `generic_write_check_limits`](https://github.com/torvalds/linux/blob/e8f897f4afef0031fe618a8e94127a0934896aba/fs/read_write.c#L1646-L1658) shortens a write that crosses the limit and, once the position is at or past it, sends `SIGXFSZ` and returns `EFBIG`; [libpcap `pcap_dump`](https://github.com/the-tcpdump-group/libpcap/blob/95691ebe7564afa3faa5c6ba0dbd17e351be455a/sf-pcap.c#L749-L797) writes the record header and then the data and stops writing once the stream is in an error state; tcpdump's [`-U` path](https://github.com/the-tcpdump-group/tcpdump/blob/032e4923e5202ea4d5a6d1cead83ed1927135874/tcpdump.c#L3076-L3091) counts the packet before it dumps and flushes it, so a packet lost to the limit still counts as captured; CPython 3.11.2 [ignores `SIGXFSZ` at initialization](https://github.com/python/cpython/blob/878ead1ac1651965126322c1b3d124faf5484dc6/Modules/signalmodule.c#L1936-L1947) and [`subprocess` restores it when `restore_signals` is set](https://github.com/python/cpython/blob/878ead1ac1651965126322c1b3d124faf5484dc6/Lib/subprocess.py#L1709-L1717).

Not done: the native orchestration (establishing the prefix with the selected traffic, choosing a limit of prefix plus 5 or 17 bytes, further traffic, `SIGTERM`), the loopback diagnostic for a tcpdump that keeps running, and any native result on either architecture. The Docker-free tests use fakes, synthetic pcap files whose last record header or body is cut, and synthetic supervisor outputs; they run no real file-size limit.

## clock-probe helper (contract 1, quick check only)

The clock-probe helper is the placement for the clock-skew and time-jump scratch work in [ceph-testcontainers-go#66](https://github.com/JSYoo5B/ceph-testcontainers-go/issues/66). That research needs a C compiler and a pinned libfaketime interposer inside an isolated scratch container, and the essential role images carry neither. The helper provides them, so that a fixture needs no toolchain on the runner, no matching of the image's libc and no network at test time. The helper contains no Ceph component. Nothing in it applies an offset to a Ceph daemon, decides what a clock fault means or qualifies a clock-fault backend: a module starts the helper as a scratch container of its own and runs its own probe in it.

This section defines contract 1 and its quick check, which [image/extension_check.py](../image/extension_check.py) runs today. No helper image is published, no functional check exists and no engine support is claimed.

### Image contract

| Requirement | Detail |
| --- | --- |
| Platform | Linux `arm64` or `amd64`. The container runs as root and needs no capability and no network. |
| Entry | The module overrides entrypoint and command; `sleep infinity` keeps an idle helper running. |
| Executables on `PATH` | `sh`, `sleep`, `python3`, `gcc`, `nm`, `clock-probe-build-info` |
| Compiler | `gcc --version` reports a version, and `gcc` builds and links a C program that calls `clock_gettime`. |
| Build manifest | `clock-probe-build-info` prints one JSON object: `schema` 1; `source` with `repository`, `tag`, `commit` and `tree` (a 40-digit commit and tree); `toolchain` with `compiler` and `libc`; `architecture` (the machine that runs the helper); and `variants` with exactly `default` and `intercept-syscall`. |
| Variants | Each variant has an absolute `path` to an ELF shared object, its `sha256`, `intercept_syscall` (a boolean) and `compile_flags` (the string `-DINTERCEPT_SYSCALL` for `intercept-syscall` and the empty string for `default`). The files differ, each hash is the hash of its file, and the two builds differ only in the `INTERCEPT_SYSCALL` compile switch. Only the variant with the switch exports `syscall`. |
| Interposer load | Each variant loads through `LD_PRELOAD` with `FAKETIME_TIMESTAMP_FILE` naming a file that the caller owns and with `FAKETIME` unset. A `+0` file leaves the public realtime clock of that process within 5 seconds of an uninstrumented reader, and a `+3600` file moves it by 3600 seconds, within 5 seconds. |
| Command supervision | `python3` imports `base64`, `ctypes`, `hashlib`, `json`, `os`, `platform`, `select`, `signal`, `subprocess`, `sys`, `tempfile` and `time`. |
| Filesystem | `/tmp` is writable. |

`clock-probe-build-info` exists so that a probe records the interposer's identity (commit, `INTERCEPT_SYSCALL` setting and hash) from the image itself and not from a tag name or a label. The reference helper also carries the labels `io.ceph-testcontainers.extension=clock-probe` and `io.ceph-testcontainers.extension.contract=1`. Other helpers do not need them, and the checker does not rely on them.

### Quick check

```sh
make clock-probe-image                    # build ceph-testcontainers:extension-clock-probe
make clock-probe-check                    # its quick check
python3 image/extension_check.py --extension clock-probe --image my-registry/clock-probe:dev
```

The quick probe ([check-clock-probe.sh](../image/extensions/clock-probe/check-clock-probe.sh)) runs as root in a container with no network and no capability. Besides the executables, compiler and manifest checks, it compiles a small program, preloads each variant with a timestamp file that the probe owns, and requires the public `clock_gettime(CLOCK_REALTIME)` of that one process to follow the `+0` and `+3600` files. `extension-report.json` keeps every check and the gcc version, and `quick.log` keeps the manifest summary and the measured offset intervals.

The load check uses a static file. It does not update a file while a process runs, so it says nothing about update latency or about restoring a running process.

### Reference helper

[image/extensions/clock-probe](../image/extensions/clock-probe) builds on the pinned `debian:bookworm-slim` base of the Debian role images with Debian's `build-essential`, `binutils` and `python3`. [build-libfaketime.sh](../image/extensions/clock-probe/build-libfaketime.sh) fetches libfaketime `v0.9.12` once during the image build as the single pinned commit `3ccdd344aadf1e317156fa5fb7c881f2c4404778`, fails the build when the fetched commit differs, builds `libfaketime.so.1` without and with `-DINTERCEPT_SYSCALL`, writes the manifest, and removes the source tree and its fetch tools. The image carries libfaketime's license (GPL-2.0) at `/usr/share/doc/libfaketime/copyright`, and `/usr/share/ceph-testcontainers/SOURCES.txt` points to the commit and, for the Debian packages, to their source packages. CI builds it and runs the quick check on native runners of both architectures; it is not published.

### What the helper does not cover

- Whether the four clock routes of the scratch probe (public `clock_gettime`, libc `syscall`, the vDSO and a raw syscall) shift or bypass the interposer. The quick check measures the public realtime path of one process only.
- A probe binary, the decoder of its output, or any native scenario of the issue. Those belong to the module and to the later steps of the issue.
- A live update of the offset file, update latency, restoration or disposal of an instrumented process.
- Any Ceph daemon or client, a time namespace, a virtual machine or a kernel clock fault.
- A functional check, publication and engine support. They need their own decision and change.
- The interposer is built for glibc on Debian. A different libc needs its own helper or a changed build, and a build on another base can change the ABI that the module sees.

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

A selector profile delays or drops only the packets that the target sends to selected peers, families and TCP ports, through an owned qdisc and filter hierarchy. Contract 1 promises these profiles: the publication gate below requires each of them, and a module may build peer-selective faults on them.

| Profile | Selects |
| --- | --- |
| `ipv4-tcp-u32` | The target's IPv4 egress to peer B, TCP, destination port 7000 |
| `ipv6-tcp-u32` | The same over IPv6, for TCP directly after the IPv6 base header |
| `multi-peer-u32` | Three leaves in one hierarchy: peer B over IPv4 on port 7000, peer C over IPv4 on port 7001, and peer B over IPv6 on port 7000 |
| `tcp-sport-u32` | The target's TCP egress to peer B from source port 7000, over IPv4 and over IPv6: what a server on port 7000 in the target sends back to peer B when peer B connects to it |
| `tcp-setup-u32` | The target's TCP segments with SYN set to peer B, destination port 7000, over IPv4 and over IPv6: the target's connection attempts on that path, while its established connections pass |

A leaf's port key is `dport` (`match ip dport 7000 0xffff`), or `sport` for `tcp-sport-u32` (`match ip sport 7000 0xffff`, which tc packs into the upper half of the destination port's word: offset 20, or 40 over IPv6, mask `ffff0000`). A `tcp-setup-u32` leaf adds `match u8 0x02 0x02 at 33` (at 53 over IPv6) after its `dport` key: the SYN bit of the TCP flags, which tc prints in the word at offset 32 (52) with value and mask `00020000`. A SYN-ACK that the target sends to a peer's own connection carries the peer's ephemeral port as its destination port, so the leaf selects the target's own connection attempts on the path.

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

1. It finds the target's interface by its address and MAC, records its root qdisc, and times each selected path. A `sport` path is a connection from the peer to a server on the port in the target's namespace; every other path is a connection from the target to the peer. For `tcp-setup-u32` it also opens one connection on each selected path, which stays open, and times requests on it.
2. It adds the root and the child only, and times each selected path again. Until a leaf exists, every packet takes the unshaped band: the paths must stay fast and the child must count no packet, so an apply that stops before its leaves delays nothing.
3. It adds the leaves in the layout's order with a delay (`--delay-ms`) and reads the filters back. tc prints one `match` member per u32 key, repeated in one object, so the checker decodes them in order instead of as a dictionary. It compares every leaf's preference, protocol, handle, flow ID and every key's offset, value, mask and offmask with the requested one, and each preference's table with the layout. tc packs keys into words: the IPv6 next header and port appear at offsets 4 and 40. Any other filter entry is foreign state.
4. It times the controls before any selected traffic: every other combination of peer, family and port in both directions, from the target to each peer on ports 7000 and 7001, and from each peer to the target's servers on ports 6999, 7000 and 7001. They must stay fast, and the child must count no packet. Then every selected path must take at least the delay, and the child must count its packets. For `tcp-setup-u32` the connection opened before the leaves must stay fast: only a new connection waits for the delay.
5. It waits, within 2 s, until the child holds no packet (backlog and queue length 0): a completed read does not show that the queue drained. The 2 s is one deadline: each `tc` read may use only what remains of it, and an empty queue read after the deadline does not count. A read that times out, or a queue that is still busy or unobserved at the deadline, fails the scenario with `drain_pending` and its reason, and the child never changes to loss. Otherwise the checker then changes the child to 100% loss. A fresh connection on every selected path must time out, the controls must still answer, and the child must count drops. For `tcp-setup-u32` the connection opened before the leaves must still answer fast: the loss drops connection setup, not an established connection.
6. It adds a filter of another owner under the root (preference 20). The readback must refuse the hierarchy as foreign state, and accept it again once that filter is removed.
7. It removes the helper and starts another in the target's namespace. Removing a helper does not remove kernel state: the new helper must find every leaf and the child with its loss.
8. It removes the leaves one by one in reverse order, then any preference whose u32 table outlived its leaves (removing the last leaf can remove its whole chain), then the root. The root qdisc must be the original one again, no filter may remain, and every selected path must be fast again.
9. It adds the whole hierarchy again and restarts the target. The target must get a new network sandbox, the helper left in the old namespace must no longer see the target's interface, and a new helper must find the original root qdisc and no filter.

`extension-report.json` keeps each profile's result under `selectors`, and each scenario directory keeps the raw filter dump (`filter.json`) and every timing. The IPv6 leaves do not select IPv6 traffic with extension headers or fragments, and no profile covers IPv4 options, IPv4-mapped addresses or receiver-side shaping. A module shapes the other direction of a path at the peer's egress instead: `tcp-sport-u32` selects, at the peer, what it sends back from its own port, so a module can shape both directions of a conversation with one of the peer's ports.

The selector profiles also need the engine's kernel to provide the prio qdisc and the u32 classifier (see [Runtime requirements](#runtime-requirements)). On an arm64 Docker Desktop engine, the published `extension-netem-v1` helper passed every selector profile against the official images of 20.2.4 and 19.2.5. With a 200 ms delay, a `tcp-setup-u32` connection took 206 ms while the connection opened before the leaves answered in 0.02 ms, also under 100% loss, and a `tcp-sport-u32` request from peer B took 407 ms (the SYN-ACK and the answer); every control stayed under 0.7 ms. Earlier, the `ipv4-tcp-u32` and `ipv6-tcp-u32` scenarios of an earlier checker version passed against the official image and the OSD role image of 20.2.4 and 19.2.5, with the published `extension-netem-v1` helper. With a 200 ms delay, the selected path took 410 to 412 ms and every control 0.06 to 0.11 ms; the child drained within 170 ms.

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

## Published helpers

Each helper tag lists `linux/amd64` and `linux/arm64`; `<tag>-linux-<architecture>` selects one platform. The contract version is part of the tag, so a later incompatible contract gets a new tag. Each helper is staged, promoted and checked again on its own: a failure of one helper never blocks the other, and neither blocks the release images.

Publication runs only from a manual run of the [checker workflow](../.github/workflows/test.yml) with `publish`. For each architecture, the job that built the helper and passed its functional check uploads that exact image under a `ci-<run>-<attempt>-` candidate tag. Promotion requires the candidates of both architectures from the same run and revision, verifies their configuration, platform, layers and extension label, points the platform tags and then the index at those digests, and checks that the index lists exactly the two tested digests. A platform whose check failed uploads no candidate, so nothing is promoted. [published-extension.yml](../.github/workflows/published-extension.yml) then pulls each platform by digest and runs the same functional check again. The weekly published image health workflow runs `python3 image/publish.py extension-health`, which fails when a helper tag no longer lists both platform images.

### netem helper

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-netem-v1
```

- Built from [image/extensions/netem](../image/extensions/netem) on the pinned `debian:bookworm-slim` base of the Debian role images, with Debian's `iproute2` and `python3` (iproute2 6.1.0 and Python 3.11.2 when this was written). The `linux/arm64` image is 45 MB compressed and 155 MB unpacked.
- `/usr/share/ceph-testcontainers/SOURCES.txt` and `source-packages.txt` list every package with its source package and version.
- CI checks the helper functionally on native runners of both architectures, with every selector profile, the bandwidth profile, the packet-fault profile, and the official image and the published OSD role image of every release in [image/releases.py](../image/releases.py) as targets. Publication requires every target, every selector profile, the bandwidth profile and the packet-fault profile to pass and clean up on both architectures.

#### Engine coverage

| Engine | Platforms | Evidence |
| --- | --- | --- |
| Docker Engine on GitHub-hosted Ubuntu 24.04 runners | `amd64`, `arm64` | Functional check with every selector profile, the bandwidth profile and the packet-fault profile before every publication and on the published digests |
| Native Linux Docker Engine | `amd64` | Functional check with every selector profile, the bandwidth profile and the packet-fault profile, with the official 20.2.4 and 19.2.5 images and their published OSD role images |
| Docker Desktop | `arm64` | Functional check with the official 20.2.4 and 19.2.5 images and their published OSD role images, and the single-family selector profiles of an earlier checker version; every selector profile, `tcp-sport-u32` and `tcp-setup-u32` included, with the official 20.2.4 and 19.2.5 images; the bandwidth and packet-fault profiles are not verified |

Other engines, rootless Docker, Podman and Docker Desktop's Enhanced Container Isolation are not covered. Run the functional check on them before relying on the helper.

### Why a helper and not extended role images

The other way to provide `tc` is to add it to the role images and run `tc` inside each daemon container.

- Every role image of every variant and release would need the tools and a separate netem tag, and an extended `all` image would have to stay the union of the extended roles.
- Every daemon container that could be a target would need `NET_ADMIN`, and the module would have to grant it to every initial and later MON, MGR, OSD, RGW, MDS, mirror and client container.
- The official image and custom Ceph images could never be targets.

The helper avoids all three, at the cost of one more small image and one more container per delayed target. No case seen so far needs the role-image route.

### packet-capture helper

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:extension-capture-v1
```

The tag exists once a manual run with `publish` on the default branch has passed; its capture jobs stage candidates only on that branch.

- Built from [image/extensions/capture](../image/extensions/capture) as described under [Reference helper](#reference-helper), with Debian's `tcpdump` and `python3`. `/usr/share/ceph-testcontainers/SOURCES.txt` and `source-packages.txt` list every package with its source package and version, and the candidate records the tcpdump and libpcap version that the quick check read.
- Publication requires, on each architecture, a passing functional report from the current checker and probe: the quick check including the refusal without `NET_RAW`, the reference labels, and for the official image of every release in [image/releases.py](../image/releases.py), a scenario with the checked helper image on that platform in which the traffic, empty, bound, abrupt, target-loss, IPv6 and restart captures pass, the invalid-filter, missing-interface and missing-`NET_RAW` controls fail with their own kinds, and cleanup leaves nothing behind.
- The published digests are checked again the same way, with the official image of every release as targets. No raw capture is uploaded at any stage (see [Raw captures](#raw-captures)).

What the publication claims: on Docker Engine on GitHub-hosted Ubuntu 24.04 runners, natively on `amd64` and `arm64`, this exact image met contract 1 and the functional scenario above in a container of the official image of each supported release, as the image's own non-root user with `NET_RAW` alone, on an internal IPv4 bridge, and for the IPv6 and restart captures on an internal dual-stack bridge.

What it does not claim:

- Any other engine: rootless Docker, Podman, Docker Desktop and its Enhanced Container Isolation, or a Linux host with a different kernel or security profile. Run the functional check there before relying on the helper.
- Targets other than the official images: the role images and custom Ceph images are not checked targets.
- What the functional check does not cover: byte limits other than the packet bound, and IPv6 extension headers other than the one Destination Options header of the `ipv6_ext` capture. A tcpdump that keeps running after a target loss or a restart and a cut final record are accepted by the checker and covered by synthetic fixtures only; the hosted runs so far ended with tcpdump exiting and no cut record.
- Targets the helper cannot safely join: host-network targets, stopped targets and targets that share another container's namespace are unsupported. The helper never falls back to a host-wide capture, and a module refuses such a target instead of capturing on the host.
- Following a restarted or replaced target: a restart gives the target a new network namespace, and the helper stays in the old one. The restart capture records that the helper did not follow, so a module that wants the traffic after a restart starts a new helper; automatic following is not part of contract 1.
- Exact byte or duration limits: the helper enforces only the packet count (`-c`) and the module's stop. The byte bound is derived from the packet count and snaplen, and the time bound is the module's own stop deadline. Rotation, several files and pcapng are not part of contract 1, so there is no ordering or overwritten interval to report.
- Interface, route, qdisc or target configuration: capture changes none of them, and it needs no `NET_ADMIN`; the helper holds `NET_RAW` alone and never runs privileged.
- Anything about the captured traffic: decoding, decrypting or judging Ceph Messenger traffic is left to the consumers.

#### Engine coverage

| Engine | Platforms | Evidence |
| --- | --- | --- |
| Docker Engine on GitHub-hosted Ubuntu 24.04 runners | `amd64`, `arm64` | Quick and functional check with the official 20.2.4 and 19.2.5 images on pull requests and pushes to the default branch, before every publication and on the published digests |

## Not in contract 1

- Loss of only some of the packets, other loss models and netem's `slot`. They use the same `tc`, but only a delay, 100% loss, the bandwidth profile's rate and the packet-fault profile's faults of selected packets are checked.
- A reorder gap other than 1, and a reorder with jitter or a rate.
- A separate rate for each selected peer, and shaping with another qdisc such as `tbf` or `htb`.
- Receiver-side delay through IFB. Selectors also do not cover IPv6 extension headers or fragments, IPv4 options or IPv4-mapped addresses.
- `ss` and `nft` for socket destruction and peer-specific filtering. Each becomes its own contract with its own checks; packet capture has its own [contract](#packet-capture-helper-contract-1).
