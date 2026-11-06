#!/usr/bin/env python3
"""Ceph releases this repository builds, checks and publishes, with their exact inputs.

Every image input that depends on the Ceph release lives in releases.json: the
official source index and its native children, and for each distribution its
pinned base image, the download.ceph.com suite, the exact package versions and
the APT metadata that establishes them. A variant/architecture cohort that has
no usable input is listed as unavailable with its reason, and a cohort whose
source executables need a newer x86-64 level names it (docs/RELEASES.md).
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import sys

DEFAULT = "20.2.4"
VARIANTS = ("official", "debian", "ubuntu")
DISTRIBUTIONS = ("debian", "ubuntu")
ARCHITECTURES = ("amd64", "arm64")
ROLES = ("essential", "client", "cephfs", "rbd-aux", "rgw", "all")
# Optional roles, published only for the listed variants: mgr-modules is
# extracted from the official image's MGR module bundle.
OPTIONAL_ROLES = {"mgr-modules": ("official",)}
RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
CATALOGUE = Path(__file__).with_name("releases.json")
INVENTORY = Path(__file__).resolve().parents[1] / ".github/ceph-role-images.json"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


def load(path=None):
    data = json.loads((path or CATALOGUE).read_text())
    if data.get("schema") != 1 or not isinstance(data.get("releases"), dict) or DEFAULT not in data["releases"]:
        raise ValueError("Invalid release catalogue")
    for name, entry in data["releases"].items():
        if not VERSION.fullmatch(name) or set(entry) != set(VARIANTS) | {"codename"}:
            raise ValueError("Invalid catalogued release: " + name)
        for variant in VARIANTS:
            value = entry[variant]
            arches = value.get("architectures")
            if (not isinstance(arches, list) or not arches or len(arches) != len(set(arches))
                    or not set(arches) <= set(ARCHITECTURES)):
                raise ValueError("Invalid catalogued architectures")
            if variant == "official":
                prefix = "quay.io/ceph/ceph:v" + name + "@"
                if not value.get("source", "").startswith(prefix) or not DIGEST.fullmatch(value["source"][len(prefix):]):
                    raise ValueError("Official input must pin its exact release digest")
                if set(value.get("children", {})) != set(arches) or any(not DIGEST.fullmatch(d) for d in value["children"].values()):
                    raise ValueError("Official platform digests are incomplete")
            else:
                if (not value.get("base", "").startswith(value.get("base_name", "") + "@")
                        or not DIGEST.fullmatch(value["base"].split("@", 1)[-1])
                        or not value.get("package", "").startswith(name + "-")
                        or value.get("suite") not in ("bookworm", "jammy", "noble")):
                    raise ValueError("Distribution input must pin its base and matching Ceph package")
                extras = value.get("mgr_packages")
                if not isinstance(extras, str):
                    raise ValueError("Manager package inputs must be explicit")
                for extra in extras.split():
                    package, separator, version = extra.partition("=")
                    if package.startswith("ceph-") and (not separator or version != value["package"]):
                        raise ValueError("Extra Ceph packages must pin the matching package version")
            baseline = value.get("cpu_baseline", {})
            if (not isinstance(baseline, dict) or not set(baseline) <= set(arches) & {"amd64"}
                    or any(not re.fullmatch(r"x86-64-v[234]", str(level)) for level in baseline.values())):
                raise ValueError("A CPU baseline names the x86-64 level of an available amd64 cohort")
            if set(value.get("unavailable", {})) != set(ARCHITECTURES) - set(arches):
                raise ValueError("Every unavailable architecture needs an explicit reason")
    return data["releases"]


RELEASES = load()


def release(name):
    if name not in RELEASES:
        raise KeyError("Unknown Ceph release " + repr(name) + "; known: " + ", ".join(RELEASES))
    return RELEASES[name]


def names():
    """Every catalogued release, the default first."""
    return [DEFAULT] + [name for name in RELEASES if name != DEFAULT]


def official(name, architecture=None):
    """The official source index, or with an architecture its pinned native child."""
    value = release(name)["official"]
    if architecture is None:
        return value["source"]
    if architecture not in value["architectures"]:
        raise KeyError("Release " + name + " has no official " + str(architecture) + " image")
    return value["source"].split("@", 1)[0] + "@" + value["children"][architecture]


def cpu_baseline(name, variant, architecture):
    """The x86-64 level a cohort's source executables require, or None."""
    return release(name)[variant].get("cpu_baseline", {}).get(architecture)


def cohorts(name):
    """The (variant, architecture) cohorts whose inputs are available."""
    entry = release(name)
    return {(variant, architecture) for variant in VARIANTS for architecture in entry[variant]["architectures"]}


def selected(value):
    """'all' or distinct comma-separated catalogued releases, in that order."""
    chosen = list(RELEASES) if value == "all" else value.split(",")
    if not chosen or len(chosen) != len(set(chosen)) or any(name not in RELEASES for name in chosen):
        raise ValueError("Select distinct catalogued releases or all")
    return chosen


def plan(value):
    """The selected releases and the native matrix of their available cohorts."""
    chosen = selected(value)
    available = {name: cohorts(name) for name in chosen}
    return {"releases": chosen, "matrix": {"include": [
        {"release": name, "variant": variant, "architecture": architecture, "runner": RUNNERS[architecture]}
        for name in chosen for variant in VARIANTS for architecture in ARCHITECTURES
        if (variant, architecture) in available[name]]}}


def roles(variant):
    """The published roles of a variant: the baseline roles, all, then its optional roles."""
    if variant not in VARIANTS:
        raise KeyError("Unknown variant " + repr(variant))
    return ROLES + tuple(role for role, variants in OPTIONAL_ROLES.items() if variant in variants)


def build_args(name, distribution):
    """Docker build arguments for one distribution image of a release."""
    if distribution not in DISTRIBUTIONS:
        raise KeyError("Unknown distribution " + repr(distribution))
    values = release(name)[distribution]
    return {"BASE_IMAGE": values["base"], "BASE_NAME": values["base_name"], "CEPH_RELEASE": name,
            "CEPH_SUITE": values["suite"], "CEPH_PACKAGE_VERSION": values["package"],
            "CEPH_MGR_PACKAGES": values["mgr_packages"]}


def inventory():
    return json.loads(INVENTORY.read_text())["images"]


def published():
    """The releases whose role indexes are recorded in the digest inventory, the default first."""
    recorded = inventory()
    return [name for name in names() if name in recorded]


def recorded_roles(name, variant):
    """The roles whose indexes the digest inventory records for a published
    release and variant. An optional role is absent until a publication of
    its release has recorded it."""
    recorded = set(inventory()[name][variant])
    expected = roles(variant)
    if not set(ROLES) <= recorded <= set(expected):
        raise ValueError("Recorded roles differ from the role contract: " + name + " " + variant)
    return tuple(role for role in expected if role in recorded)


def published_role(name, variant, role):
    release(name)
    if role not in roles(variant):
        raise KeyError("Variant " + variant + " publishes no " + role + " role")
    digest = inventory()[name][variant][role]
    if not DIGEST.fullmatch(digest):
        raise ValueError("Invalid published role digest")
    return "ghcr.io/jsyoo5b/ceph-testcontainers-images@" + digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Print every catalogued release, default first")
    commands.add_parser("published", help="Print every release with published role images, default first")
    official_parser = commands.add_parser("official", help="Print the official source image of a release")
    official_parser.add_argument("release")
    official_parser.add_argument("architecture", nargs="?", choices=ARCHITECTURES,
                                 help="Print the pinned native child instead of the index")
    arguments = commands.add_parser("build-args", help="Print docker --build-arg options as shell words")
    arguments.add_argument("release")
    arguments.add_argument("distribution", choices=DISTRIBUTIONS)
    role = commands.add_parser("role", help="Print an immutable published role reference")
    role.add_argument("release")
    role.add_argument("variant", choices=VARIANTS)
    role.add_argument("role", choices=ROLES + tuple(OPTIONAL_ROLES))
    plan_parser = commands.add_parser("plan", help="Print the releases and native matrix of available cohorts")
    plan_parser.add_argument("--releases", default=DEFAULT, help="all or comma-separated catalogued releases")
    plan_parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "list":
        print("\n".join(names()))
    elif args.command == "published":
        print("\n".join(published()))
    elif args.command == "official":
        print(official(args.release, args.architecture))
    elif args.command == "role":
        print(published_role(args.release, args.variant, args.role))
    elif args.command == "plan":
        value = plan(args.releases)
        if args.github_output:
            with args.github_output.open("a") as output:
                for field in ("releases", "matrix"):
                    output.write(field + "=" + json.dumps(value[field], separators=(",", ":")) + "\n")
        print(json.dumps(value))
    else:
        # Shell words: a manager package list holds spaces.
        print(" ".join("--build-arg " + shlex.quote(key + "=" + value)
                       for key, value in build_args(args.release, args.distribution).items() if value))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyError as error:
        print(error.args[0], file=sys.stderr)
        sys.exit(2)
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(2)
