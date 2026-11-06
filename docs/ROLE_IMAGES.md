# Building role images

See [Roles](ROLES.md) for the runtime capability boundaries and existing test
mapping, [Image Requirements](IMAGE_REQUIREMENTS.md) for common requirements,
and the [README](../README.md#role-images) for build and review CI commands.

`make role-images VARIANT=official` extracts the pinned installed runtime into
shared layers. `CEPH_RELEASE` selects any catalogued release
([Releases](RELEASES.md)); the official variant extracts that release's pinned
native child for `PLATFORM`. Debian, Ubuntu and experimental Alpine use the same assembler
with their package backend. The official variant also extracts the optional
mgr-modules image: ceph-mgr with every MGR module that the release ships and
their dependency closure ([Roles](ROLES.md#the-mgr-modules-role)). `make role-images-full` adds complete functional
checks. Publication is gated by the image requirement (quick) check; the
functional scenarios are recorded as auxiliary evidence
([Releases](RELEASES.md#building-checking-and-publishing)). Images contain runtime tools; consumers supply bootstrap and lifecycle.
