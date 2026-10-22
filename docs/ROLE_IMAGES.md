# Building role images

See [Roles](ROLES.md) for the runtime capability boundaries and existing test
mapping, [Image Requirements](IMAGE_REQUIREMENTS.md) for common requirements,
and the [README](../README.md#role-images) for build and review CI commands.

`make role-images VARIANT=official` extracts the pinned installed runtime into
shared layers. Debian, Ubuntu and experimental Alpine use the same assembler
with their package backend. `make role-images-full` adds complete functional
checks. Images contain runtime tools; consumers supply bootstrap and lifecycle.
