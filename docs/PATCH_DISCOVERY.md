# Patch discovery images

Historical comparison uses separate images; it does not extend production
support or ordinary PR acceptance. The production release list, full functional
publication gate and pinned production digests remain unchanged.

The opt-in `discovery` mode of the existing `images` workflow builds the five
roles (`essential`, `client`, `cephfs`, `rbd-aux`, `rgw`) and aggregate `all`
for every selected patch and eligible native architecture. It packs sequential
patch builds into six native jobs, one per variant/architecture. Each patch's
promotion needs every eligible cohort in the committed input inventory, even
if another patch in the same packed job fails.

## Published snapshot

The November 2, 2026 campaign published all eight Squid and five Tentacle
patches listed below: 438 native platform images and 234 role indexes across
official, Debian and Ubuntu variants. The
[digest inventory](../.github/ceph-discovery-images.json) records index digests,
native children, observed Ceph builds and per-patch publication-run provenance.
Only patches whose promotion and registry health checks passed appear there.

An additional native ARM check used the exact published official 19.2.3
digests. RBD, CephFS, RGW S3 and the `hello` object-class scenario passed for
both the aggregate image and a cluster composed of separate role images,
including owned cleanup. This selected functional evidence is recorded in
the digest inventory; it does not qualify every scenario or every patch.

Umbrella 21.2.0 remains **unpublished**. Preparation handles its OSD alternatives
and split mirroring package. A subsequent official AMD64 probe passed all six
builds and smoke checks, but essential/all quick checks still failed because
the required MGR `snap_schedule` module was absent. Further Umbrella adaptation
and the remaining native qualification were deferred; available upstream
images/packages are not a passing role-image result.

## Inputs and eligibility

[The input catalogue](../image/patch-releases.json) pins actual upstream OCI
indexes and native children, distribution bases, package versions and the
APT metadata used to establish availability. Its 14 patch versions are:

- Squid: 19.2.0 through 19.2.7.
- Tentacle: 20.2.0 through 20.2.4.
- Umbrella: 21.2.0.

All official sources have amd64 and arm64 children. Debian Bookworm and Ubuntu
Jammy/Noble inputs identify 79 input-eligible native cohorts, representing 474
potential platform role images before native qualification. The published
snapshot above excludes Umbrella's six unqualified cohorts. The exceptions to
input eligibility include Debian 19.2.0–19.2.3 arm64: the required
matching-release packages are absent, and alternative Debian suites return
404. These four cohorts remain explicitly unavailable; no source build or
substitution with another distribution/release is performed.

Ubuntu 19.2.6 arm64 has a different input failure: its signed Jammy metadata
requires a 437982-byte `libradosstriper1` archive with SHA256 `f9503cf1…`,
but canonical and cache-bypassed downloads both return 437970 bytes with
SHA256 `78d35ff9…`. The current Release signature and metadata hashes verify;
the package bytes do not. Noble is unavailable. The catalogue retains full
expected/observed identities and the original failed build run. This cohort
remains externally blocked, and its Ubuntu index has only the checked amd64
child. No integrity check is disabled to use the mismatching archive.

Early Debian packages use `19.2.x-1~bpo12+1`, rather than the newer
`-1bookworm` suffix. Ubuntu Squid uses Jammy. Tentacle 20.2.0 and 20.2.3 also
use Jammy because their Noble repositories are absent; 20.2.1, 20.2.2 and
20.2.4 use Noble. Umbrella uses Bookworm/Noble for the distribution variants.
Its official Rocky Linux 10 AMD64 source requires an x86-64-v3 CPU; the build
preflight checks eligibility before interpreting source execution failures.
Umbrella separates common OSD files from the classic implementation. Extraction
preserves the installed `ceph-osd` alternatives link chain and its
`ceph-osd-classic` target in the essential role, keeping the public entrypoint.
Distribution inputs explicitly install the matching-version
`ceph-mgr-mirroring` package; its split module is selected into essential
alongside the core manager modules.

An upstream source Git tag, registry image, APT package and native qualification
are separate observations. Umbrella's registry and distribution artifacts were
available at inventory time even though the queried Git source tag was not.
Reports retain actual native version/build and immutable image identities.
Some older official images omit Ceph COPYING although RPM metadata names it.
The assembler can restore only the vendored upstream text pinned to the exact
observed Ceph build commit and SHA256; existing license files are preserved.
Every affected role records the addition in `license-supplement.json`. No Ceph
executable is patched and the assembly remains offline.

## Qualification and publication

Each image must pass the maintained role extraction, smoke test and quick
runtime contract on its native architecture. Quick checks establish required
executables, libraries, bindings, observed Ceph release/build and cleanup.
They **do not** establish cluster interoperability or resolve known historical
Ceph failures, such as Squid 19.2.6 RGW realm bootstrap behavior.
The glibc object-class quick probe checks ELF dependencies and strong symbol
relocation without running pre-main constructors or IFUNC resolvers. Normal
tool/version execution is checked separately; class initialization and methods
need functional evidence. The native musl preload probe is preserved.

Only the exact checked local config/layer identities are staged. Promotion
verifies registry configs/layers and creates indexes containing exactly the
eligible checked native children. A failed eligible cohort cannot be omitted
to make a patch publish. Known unavailable inputs are recorded before building
and are never counted as passed checks. Discovery candidates cannot enter the
production promotion path.

Published names are:

```text
ghcr.io/jsyoo5b/ceph-testcontainers-images:discovery-<variant>-<patch>-<role>
ghcr.io/jsyoo5b/ceph-testcontainers-images:discovery-<variant>-<patch>-<role>-linux-<architecture>
```

Variants are `official`, `debian` and `ubuntu`. Debian's four old versions and
Ubuntu 19.2.6 intentionally have no arm64 child. Consumers should retain the digest
and actual build evidence from the promotion artifacts, rather than infer a
support guarantee from a version or tag name.

For a manual campaign select workflow `images`, mode `discovery`, patches
`all` or a comma-separated subset, and `publish=true`. The default mode stays
`production`; no periodic job or downstream supported-release selector changes.

```sh
python3 image/discovery.py plan --releases all
python3 image/discovery.py build --releases 19.2.0,21.2.0 \
  --variant official --architecture amd64 --revision <commit-sha> \
  --run-id <run-id>-<attempt> --output artifacts/discovery
```

Building alone does not publish. The explicit `--publish` flag stages checked
images. The workflow then promotes each complete patch and performs registry
health checks. Build, check, upload, promotion and health failures stay distinct
in the existing build/check/candidate/promotion reports. There is no
repeated-until-green execution. Interrupted publication must be inspected before
any retry.

Per-version private builders/cache and owned temporary image tags bound disk
use without globally pruning unrelated Docker resources. Raw image archives
are not retained as workflow artifacts; reports, package/source manifests,
native checks, candidate digests and promotion receipts are retained instead.
