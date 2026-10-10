#!/usr/bin/env python3
"""Ceph releases this repository checks and publishes, with their exact inputs.

Every image input that depends on the Ceph release lives here: the official
source image, and for each distribution its pinned base image, the suite that
download.ceph.com builds for that release, and the exact package version.
"""
import argparse
import sys

DEFAULT = "20.2.4"

RELEASES = {
    "20.2.4": {
        "codename": "tentacle",
        "official": "quay.io/ceph/ceph:v20.2.4@sha256:6bb1c8a42fbc0bf87938946990b65174466997bc11c31eb5a323225a779fd8f9",
        "debian": {
            "base": "debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251",
            "base_name": "debian:bookworm-slim",
            "suite": "bookworm",
            "package": "20.2.4-1bookworm",
        },
        "ubuntu": {
            "base": "ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55",
            "base_name": "ubuntu:24.04",
            "suite": "noble",
            "package": "20.2.4-1noble",
        },
    },
    # 19.2.6 is skipped: its radosgw-admin signs realm pull requests that the
    # same release's RGW rejects ('x-amz-content-sha256' outside
    # SignedHeaders), so RGW multisite cannot start.
    "19.2.5": {
        "codename": "squid",
        "official": "quay.io/ceph/ceph:v19.2.5@sha256:1bb011052bc6d347d3418adcbf7d88156860d45697bc6323594a11410084064b",
        "debian": {
            "base": "debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251",
            "base_name": "debian:bookworm-slim",
            "suite": "bookworm",
            "package": "19.2.5-1bookworm",
        },
        # download.ceph.com has no Noble build of Squid; Jammy is the newest.
        "ubuntu": {
            "base": "ubuntu:22.04@sha256:5ec03bb3441e8b0bf3b4f9cd4629a1ae763010dc3035bb8da3ae6cf026486401",
            "base_name": "ubuntu:22.04",
            "suite": "jammy",
            "package": "19.2.5-1jammy",
        },
    },
}

DISTRIBUTIONS = ("debian", "ubuntu")


def release(name):
    if name not in RELEASES:
        raise KeyError("Unknown Ceph release " + repr(name) + "; known: " + ", ".join(RELEASES))
    return RELEASES[name]


def build_args(name, distribution):
    """Docker build arguments for one distribution image of a release."""
    if distribution not in DISTRIBUTIONS:
        raise KeyError("Unknown distribution " + repr(distribution))
    values = release(name)[distribution]
    return {"BASE_IMAGE": values["base"], "BASE_NAME": values["base_name"], "CEPH_RELEASE": name,
            "CEPH_SUITE": values["suite"], "CEPH_PACKAGE_VERSION": values["package"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Print every release, default first")
    official = commands.add_parser("official", help="Print the official source image of a release")
    official.add_argument("release")
    arguments = commands.add_parser("build-args", help="Print docker --build-arg options")
    arguments.add_argument("release")
    arguments.add_argument("distribution", choices=DISTRIBUTIONS)
    args = parser.parse_args(argv)
    if args.command == "list":
        print("\n".join([DEFAULT] + [name for name in RELEASES if name != DEFAULT]))
    elif args.command == "official":
        print(release(args.release)["official"])
    else:
        print(" ".join("--build-arg " + key + "=" + value for key, value in build_args(args.release, args.distribution).items()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyError as error:
        print(error.args[0], file=sys.stderr)
        sys.exit(2)
