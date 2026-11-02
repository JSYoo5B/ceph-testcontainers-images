"""Pinned inputs for opt-in historical discovery, separate from production."""
import json
from pathlib import Path
import re

VARIANTS = ("official", "debian", "ubuntu")
ARCHITECTURES = ("amd64", "arm64")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"(?:19\.2\.[0-7]|20\.2\.[0-4]|21\.2\.0)\Z")


def load(path=None):
    data = json.loads((path or Path(__file__).with_name("patch-releases.json")).read_text())
    if data.get("schema") != 1 or not isinstance(data.get("releases"), dict) or not data["releases"]:
        raise ValueError("Invalid discovery release catalogue")
    for name, entry in data["releases"].items():
        if not VERSION.fullmatch(name) or set(entry) != set(VARIANTS) | {"codename"}:
            raise ValueError("Invalid discovery release: " + name)
        for variant in VARIANTS:
            value = entry[variant]
            arches = value.get("architectures")
            if (not isinstance(arches, list) or not arches or len(arches) != len(set(arches))
                    or not set(arches) <= set(ARCHITECTURES)):
                raise ValueError("Invalid discovery architectures")
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
            if set(value.get("unavailable", {})) != set(ARCHITECTURES) - set(arches):
                raise ValueError("Every unavailable architecture needs an explicit reason")
    return data["releases"]


PATCHES = load()


def release(name):
    if name not in PATCHES:
        raise ValueError("Unknown discovery patch: " + str(name))
    return PATCHES[name]


def expected_cohorts(name):
    entry = release(name)
    return {(variant, architecture) for variant in VARIANTS
            for architecture in entry[variant]["architectures"]}


def selected(value):
    names = list(PATCHES) if value == "all" else value.split(",")
    if not names or len(names) != len(set(names)) or any(name not in PATCHES for name in names):
        raise ValueError("Select distinct catalogue patches or all")
    return names


def build_args(name, variant):
    if variant not in ("debian", "ubuntu"):
        raise ValueError("Distribution build arguments require debian or ubuntu")
    value = release(name)[variant]
    return {"BASE_IMAGE": value["base"], "BASE_NAME": value["base_name"],
            "CEPH_RELEASE": name, "CEPH_SUITE": value["suite"],
            "CEPH_PACKAGE_VERSION": value["package"], "CEPH_MGR_PACKAGES": value["mgr_packages"]}
