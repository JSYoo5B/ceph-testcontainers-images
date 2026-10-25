# Auditing capture publication evidence

`image/capture_qualification.py` joins the existing capture publication outputs.
It is an offline consistency check: it neither publishes nor pulls an image,
starts a container, runs a capture, contacts GitHub/registry APIs, or decodes
Messenger traffic. A synthetic passing fixture is not native publication
evidence. A qualified result refers to the supplied immutable records, not the
current contents of a moving tag or a cryptographic CI attestation.

## Inputs

Retain one selected revision and one `run_id-attempt` before gathering inputs.
The selected checker source must be present alongside this auditor; reports
must match its checker/probe hash. Use a completed manual default-branch checker
run for publication qualification. A push or PR report can supply partial
observations but cannot qualify the publication.

Put these independently retained inputs in one directory:

| File | Source and binding |
| --- | --- |
| `run.json` | GitHub Actions run API record: repository, head SHA, run ID and selected attempt. |
| `jobs.json` | Complete selected-attempt jobs API response. Each producer must have the same source/run/attempt; job and required steps must actually pass. |
| `artifacts.json` | Complete run artifacts API response, including IDs, SHA-256 digests, sizes, expiration and creation timestamps. |
| `<artifact-id>.zip` | Original downloaded ZIP bytes whose checksum and size match that API entry. Do not rezip extracted files. |
| `index.json` | Exact registry response bytes for the index digest recorded in `promotion.json`. |
| `manifest-amd64.json`, `manifest-arm64.json` | Exact response bytes for the promoted platform manifest digests. |
| `config-amd64.json`, `config-arm64.json` | Exact response bytes for their referenced config digests. |

Fetch jobs using the selected attempt's endpoint, for example
`repos/JSYoo5B/ceph-testcontainers-images/actions/runs/RUN/attempts/ATTEMPT/jobs`.
Follow pagination and combine every returned job into one response with the
correct `total_count`. Do the same for the artifacts endpoint. An artifact API
entry does not identify its attempt directly: the auditor additionally requires
its creation timestamp to fall within the selected producer job's execution.
Candidate and promotion records must carry the exact `RUN-ATTEMPT` identity.
Ambiguous duplicate artifacts are refused rather than choosing the newest name.

The ZIP names are the existing workflow artifacts:

- `extension-capture-<arch>-check`: original functional report;
- `extension-candidate-capture-<arch>`: staged candidate;
- `extension-capture-promotion`: promotion record;
- `published-capture-extension-<arch>-check`: consumer-style digest recheck.

The capture jobs and their report uploads must be successful, including the
raw-capture guard. Unrelated extension job failure does not substitute for, or
invalidate, a successful capture chain; its workflow result remains in the
independently retained API input. A skipped capture job or guard never counts.

Obtain OCI response bytes by immutable digest, including the original bytes
used to compute each digest. Do not pretty-print, normalize whitespace or append
newlines before auditing. A digest copied from today's tag cannot replace a
missing promotion or candidate. The index must name exactly linux/amd64 and
linux/arm64, with the candidates' platform manifests. Each manifest/config hash,
platform, config link, rootfs diff-ID list and capture contract label is checked.

## Join and result

The auditor reuses the maintained publication report gate: quick checks,
permission refusal, every required capture/control, helper identity/platform,
official release target identities and cleanup must pass. It then checks:

1. The candidate's report checksum against the original report's exact bytes,
   source/run/attempt/platform and tested helper identity.
2. Both candidates against the promotion's platform and index digests, with
   candidate jobs preceding promotion and published checks following it.
3. The published report's digest reference and actual helper image identity,
   its digest-map input, target image identities and its own functional results.
4. API artifact identity/checksum, producer execution interval, raw-capture guard
   and successful report upload independently from local JSON creation.

Run it with explicit expected identities:

```sh
python3 image/capture_qualification.py --directory artifacts/capture-publication \
  --revision FULL_COMMIT_SHA --run-id RUN-ATTEMPT \
  --output artifacts/capture-qualification.json
```

The result schema is `capture-publication-qualification/v1` with evidence class
`offline-artifact-consistency`. `qualified` exits zero only when the complete
chain is consistent on both platforms. Missing/unexecuted evidence produces
`incomplete`; inconsistent or failed evidence produces `failed`; both exit one.
Invalid invocation or an output write failure exits two. Partial original report
observations remain listed, with no inferred promotion or fresh native pass.
The result records input checksums and compact target/partial-branch observations;
it does not copy arbitrary logs, image environment values or packet data.

An exited target-loss path, a kept-running path, a cut final record and
`cut_tail=false` remain their actual observed branches. One cannot supply another
branch's native evidence. No additional helper/runtime byte-bound contract,
other-engine support, downstream Go support or protocol interpretation follows
from this join.

## Bounds and fixtures

Each JSON/member is at most 8 MiB; each ZIP is at most 32 MiB with at most 128
members and 64 MiB expanded content. Input metadata is capped at 128 jobs and
128 artifacts. ZIPs are read without extraction; traversal, symlinks, encrypted
members, duplicate report members and raw-pcap names/magic are refused. Error
messages are bounded to 1024 characters. The input directory must remain stable
during the audit; the tool does not attest API record authenticity or acquire an
atomic snapshot of local files.

```sh
python3 -m unittest discover -s image/extensions/tests -p test_capture_qualification.py
make extension-test
```

Fixture coverage is Docker-free and synthetic. Actual publication qualification
requires the corresponding retained reports, registry response bytes, API records
and original artifact ZIPs; an otherwise green checker run with publication jobs
skipped remains partial evidence.

The existing main push [run 40001330263, attempt 1](https://github.com/JSYoo5B/ceph-testcontainers-images/actions/runs/40001330263)
at `c44540d30ee06ef9acca34f2c1d14fcbc455d350` provides a partial replay control.
Its original capture ZIPs (artifact IDs `12522795749` and `12521498909`) bind
successful amd64/arm64 functional reports to their API checksums, producer jobs,
source, guard and retained upload. The auditor reports `incomplete`: it was a
push, and the candidate-upload steps did not run. Those reports establish neither
promotion nor a published-digest recheck. This retrospective artifact replay is
separate from a new native execution or a live registry observation.
