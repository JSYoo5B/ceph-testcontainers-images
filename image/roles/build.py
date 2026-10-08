#!/usr/bin/env python3
"""Extract five local Ceph role images from the official Ceph image.

Only Docker and Python 3.9+ are required on the host. After the source pull,
package inspection and assembly run offline inside the source image.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import uuid


ROLES = ("control", "osd", "rgw", "mds", "all")
HERE = Path(__file__).resolve().parent
PROJECT_URL = "https://github.com/JSYoo5B/ceph-testcontainers-images"
NOTICE = "/usr/share/ceph-testcontainers/SOURCES.txt"
PROJECT = HERE.parent.parent


class BuildError(RuntimeError):
    pass


def run(args, log=None, stdin=None, capture=False, env=None, cwd=None):
    """Never interpolate image names or paths into a host shell."""
    if capture:
        result = subprocess.run(args, check=False, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=cwd)
        if result.returncode:
            raise BuildError("Command failed: " + " ".join(args) + "\n" + result.stderr.strip())
        return result.stdout.strip()
    with open(log, "w") if log else tempfile.TemporaryFile(mode="w+") as output:
        with open(stdin, "rb") if stdin else tempfile.TemporaryFile() as input_file:
            with subprocess.Popen(args, stdin=input_file, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, errors="replace",
                                  env=env, cwd=cwd) as proc:
                try:
                    for line in proc.stdout:
                        print(line, end="", flush=True)
                        output.write(line)
                    code = proc.wait()
                except BaseException:
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    raise
            if code:
                raise BuildError("Command failed (%d): %s; log: %s" % (code, " ".join(args), log))
    return ""


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def inspect_image(image, platform=None):
    original = json.loads(run(["docker", "image", "inspect", image], capture=True))[0]
    inspected = original
    if platform:
        try:
            inspected = json.loads(run(["docker", "image", "inspect", "--platform", platform,
                                         original["Id"]], capture=True))[0]
        except BuildError as error:
            if "unknown flag: --platform" not in str(error):
                raise
        actual = inspected["Os"] + "/" + inspected["Architecture"]
        if actual != platform:
            raise BuildError("Image platform differs from requested platform: " + actual)
    return inspected


def pull_image(image, args, log):
    if not args.skip_pull:
        command = ["docker", "pull"]
        if args.platform:
            command += ["--platform", args.platform]
        run(command + [image], log=log)


def archive_path(value, prefix):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 2:
        raise BuildError("Invalid archive path in assembly plan: " + value)
    if path.parts[0] != prefix or path.suffix != ".tar":
        raise BuildError("Invalid archive path in assembly plan: " + value)
    return path.as_posix()


def validate_runtime_env_entry(name, value):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise BuildError("Invalid runtime environment name: " + str(name))
    if not isinstance(value, str) or any(unicodedata.category(char) in ("Cc", "Cf", "Cs") or
                                         char in "\u2028\u2029" for char in value):
        raise BuildError("Runtime environment values cannot contain control characters: " + name)


def parse_runtime_env(specifications):
    supplied = {}
    for specification in specifications:
        name, separator, value = specification.partition("=")
        if not separator:
            raise BuildError("Runtime environment requires NAME=VALUE")
        validate_runtime_env_entry(name, value)
        if name in supplied:
            raise BuildError("Runtime environment name supplied twice: " + name)
        supplied[name] = value
    return supplied


def runtime_env_lines(supplied):
    lines = []
    for name, value in supplied.items():
        validate_runtime_env_entry(name, value)
        # Dockerfile ENV has its own lexer/variable expansion, not JSON string
        # semantics. Escape backslash first, then quote and dollar explicitly.
        encoded = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        lines.append('ENV ' + name + '="' + encoded + '"')
    return lines


def check_runtime_env(image, supplied):
    if not supplied:
        return {}
    config = image.get("Config")
    entries = config.get("Env") if isinstance(config, dict) else None
    if not isinstance(entries, list):
        raise BuildError("Built image has no inspectable runtime environment")
    observed = {}
    for entry in entries:
        if not isinstance(entry, str):
            raise BuildError("Malformed built image runtime environment")
        name, separator, value = entry.partition("=")
        if name in supplied:
            if not separator or name in observed:
                raise BuildError("Ambiguous built image runtime environment: " + name)
            observed[name] = value
    for name, expected in supplied.items():
        if name not in observed or observed[name] != expected:
            raise BuildError("Built image runtime environment differs from requested value: " + name)
    return observed


def dockerfile(plan, runtime_env=None):
    """Reuse identical independent tar layers across every target, including all."""
    groups = plan["groups"]
    ordered = plan["ordered_groups"]
    if not ordered or ordered[0] != "common" or set(ordered) != set(groups):
        raise BuildError("Assembly plan must start with a single common group")
    for name in ordered:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise BuildError("Invalid group name: " + name)
    common_tar = archive_path(groups["common"]["archive"], "groups")
    lines = [
        "# Generated by image/roles/build.py; input paths are Linux-created tar archives.",
        "FROM scratch AS common",
        "ADD --link %s /" % common_tar,
        'ENV PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8',
        'CMD ["/bin/bash"]',
        "LABEL org.opencontainers.image.base.name=" + json.dumps(plan["source_image"]),
        "LABEL org.opencontainers.image.source=" + json.dumps(PROJECT_URL),
        "LABEL org.opencontainers.image.description=" + json.dumps(
            "Ceph role image extracted from the official image; packages keep their own licenses, "
            "see " + NOTICE + " for licenses and corresponding source"),
        "LABEL io.ceph-testcontainers.source-notice=" + json.dumps(NOTICE),
    ]
    lines += runtime_env_lines(runtime_env or {})
    for role in ROLES:
        selected = plan["roles"][role]["groups"]
        if selected != [group for group in ordered if group in selected] or selected[0] != "common":
            raise BuildError("Invalid group ordering for role " + role)
        lines += ["", "FROM common AS " + role]
        for group in selected[1:]:
            lines.append("ADD --link %s /" % archive_path(groups[group]["archive"], "groups"))
        lines.append("ADD --link %s /" % archive_path(plan["roles"][role]["manifest_archive"], "manifests"))
        lines += [
            "LABEL org.opencontainers.image.title=" + json.dumps("Ceph testcontainers " + role),
            "LABEL org.opencontainers.image.version=" + json.dumps(plan["ceph_version"]),
            "LABEL io.ceph-testcontainers.role=" + json.dumps(role),
        ]
    return "\n".join(lines) + "\n"


def check_layers(plan, images):
    by_group = {}
    uses = {}
    for role in ROLES:
        selected = plan["roles"][role]["groups"]
        layers = images[role]["rootfs_diff_ids"]
        if len(layers) != len(selected) + 1:
            raise BuildError("Unexpected filesystem layer count for " + role)
        for group, diff_id in zip(selected, layers):
            if group in by_group and by_group[group] != diff_id:
                raise BuildError("Common payload layer was not reused: " + group)
            by_group[group] = diff_id
        for diff_id in layers:
            uses.setdefault(diff_id, []).append(role)
    return {
        "group_diff_ids": by_group,
        "diff_id_roles": uses,
        "unique_diff_id_count": len(uses),
        "shared_payload_group_count": sum(len(g["members"]) > 1 for g in plan["groups"].values()),
        "note": "DiffID verifies identical unpacked layer content; local snapshot usage and registry transfer are not measured",
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-image", required=True, help="Official Ceph image tag or immutable digest")
    parser.add_argument("--repository", default="ceph-testcontainers", help="Local output repository; no push is performed")
    parser.add_argument("--tag", help="Output tag prefix; defaults to official-<Ceph release>")
    parser.add_argument("--platform", help="One platform per run, e.g. linux/arm64 or linux/amd64")
    parser.add_argument("--output-dir", type=Path, help="Empty directory for manifests, build/smoke logs and report")
    parser.add_argument("--skip-pull", action="store_true", help="Use an already cached source image")
    parser.add_argument("--skip-smoke", action="store_true", help="Record smoke validation as skipped")
    parser.add_argument("--check", choices=("quick", "full"),
                        help="Run image/check.py on the built images: quick checks requirements, "
                             "full also runs every functional scenario")
    parser.add_argument("--keep-context", action="store_true", help="Keep generated tar build context in the output directory")
    parser.add_argument("--runtime-env", action="append", default=[], metavar="NAME=VALUE",
                        help="Explicit runtime ENV for every role image; repeatable, no default overrides")
    args = parser.parse_args(argv)
    try:
        args.runtime_env = parse_runtime_env(args.runtime_env)
    except BuildError as error:
        parser.error(str(error))
    return args


def check_command(tags, level, output):
    """image/check.py invocation for the built mixed roles and all image."""
    command = [sys.executable, str(PROJECT / "image" / "check.py"), "--output-dir", str(output)]
    for role in ROLES:
        command += ["--image", role + "=" + tags[role]]
    return command + (["--full"] if level == "full" else [])


def main():
    args = arguments()
    if not shutil.which("docker"):
        raise BuildError("docker is required")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output_dir or PROJECT / "artifacts" / ("roles-" + timestamp + "-" + uuid.uuid4().hex[:8])).resolve()
    if output.exists() and any(output.iterdir()):
        raise BuildError("Output directory must be empty: " + str(output))
    output.mkdir(parents=True, exist_ok=True)
    report = {"status": "running", "source_input": args.source_image, "started_at_utc": timestamp,
              "runtime_env": args.runtime_env,
              "output_directory": str(output), "check_level": args.check, "checks": {
                  "runtime_env": "pending" if args.runtime_env else "not_requested",
                  "smoke": "skipped" if args.skip_smoke else "pending",
                  "image_check": "pending" if args.check else "not_requested",
              }}
    save_json(output / "build-report.json", report)
    container_id = None
    try:
        run(["docker", "version"], log=output / "docker-version.log")
        run(["docker", "buildx", "version"], log=output / "buildx-version.log")
        pull_image(args.source_image, args, output / "source-pull.log")
        source = inspect_image(args.source_image, args.platform)
        immutable = args.source_image if "@sha256:" in args.source_image else next(iter(source.get("RepoDigests", [])), source["Id"])
        platform = args.platform or "linux/" + source["Architecture"]
        if source["Os"] != "linux":
            raise BuildError("The source must be a Linux Ceph image")
        report["source"] = {"resolved_image": immutable, "image_id": source["Id"], "platform": platform,
                            "architecture": source["Architecture"], "local_size_bytes": source["Size"]}
        save_json(output / "source-image.json", source)
        with tempfile.TemporaryDirectory(prefix="ceph-roles-") as work:
            work = Path(work)
            context = work / "context"
            context.mkdir()
            scripts = work / "ceph-roles"
            scripts.mkdir()
            for name in ("assemble.py", "analyze.py", "package_roles.py"):
                shutil.copy2(HERE / name, scripts / name)
            create = ["docker", "create", "--network=none", "--platform", platform,
                      "--label", "io.ceph-testcontainers.role-build=" + timestamp,
                      "-e", "SOURCE_IMAGE=" + immutable, "-e", "SOURCE_IMAGE_ID=" + source["Id"]]
            create += ["--entrypoint", "python3", immutable, "/tmp/ceph-roles/package_roles.py"]
            container_id = run(create, capture=True)
            run(["docker", "cp", str(scripts), container_id + ":/tmp/ceph-roles"], log=output / "source-copy.log")
            print("Assembling package closures and shared file groups...", flush=True)
            run(["docker", "start", "-a", container_id], log=output / "assembly.log")
            exit_code = run(["docker", "inspect", "--format", "{{.State.ExitCode}}", container_id], capture=True)
            if exit_code != "0":
                raise BuildError("Source assembly failed with exit code " + exit_code)
            run(["docker", "cp", container_id + ":/role-output/plan.json", str(context / "plan.json")])
            plan = json.loads((context / "plan.json").read_text())
            if plan["source_image"] != immutable or plan["oci_architecture"] != source["Architecture"]:
                raise BuildError("Assembly source/architecture differs from the inspected source image")
            release = re.match(r"ceph version (\S+)", plan["ceph_version"])
            if not release:
                raise BuildError("Unexpected Ceph version output: " + plan["ceph_version"])
            tag = args.tag or "official-" + release.group(1)
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,119}", tag):
                raise BuildError("Invalid output tag prefix; specify --tag")
            tags = {role: "%s:%s-%s" % (args.repository, tag, role) for role in ROLES}
            report.update({"ceph_version": plan["ceph_version"], "tags": tags})
            save_json(output / "plan.json", plan)
            archives = {archive_path(g["archive"], "groups") for g in plan["groups"].values()}
            archives.update(archive_path(plan["roles"][role]["manifest_archive"], "manifests") for role in ROLES)
            for archive in sorted(archives):
                target = context / archive
                target.parent.mkdir(exist_ok=True)
                run(["docker", "cp", container_id + ":/role-output/" + archive, str(target)])
            run(["docker", "rm", container_id])
            container_id = None
            generated = dockerfile(plan, args.runtime_env)
            (context / "Dockerfile").write_text(generated)
            (output / "Dockerfile.generated").write_text(generated)
            report["images"] = {}
            if args.runtime_env:
                report["checks"]["runtime_env"] = "running"
            for role in ROLES:
                print("Building " + tags[role], flush=True)
                run(["docker", "buildx", "build", "--load", "--network=none", "--platform", platform,
                     "--provenance=false", "--target", role, "-t", tags[role], str(context)], log=output / ("build-" + role + ".log"))
                built = inspect_image(tags[role], platform)
                observed_runtime_env = check_runtime_env(built, args.runtime_env)
                report["images"][role] = {"tag": tags[role], "image_id": built["Id"], "architecture": built["Architecture"],
                                           "local_size_bytes": built["Size"], "rootfs_diff_ids": built["RootFS"]["Layers"],
                                           "runtime_env": observed_runtime_env,
                                           "logical_regular_file_bytes": plan["roles"][role]["logical_regular_file_bytes"]}
                save_json(output / "build-report.json", report)
            if args.runtime_env:
                report["checks"]["runtime_env"] = "passed"
            report["layer_sharing"] = check_layers(plan, report["images"])
            if args.keep_context:
                shutil.copytree(context, output / "context")
        if args.skip_smoke:
            report["checks"]["smoke"] = "skipped"
        else:
            report["checks"]["smoke"] = "running"
            save_json(output / "build-report.json", report)
            for role in ROLES:
                print("Smoke testing " + role, flush=True)
                run(["docker", "run", "--rm", "-i", "--network=none", "--platform", platform,
                     "--entrypoint", "/bin/sh", tags[role], "-s", "--", role],
                    stdin=HERE / "smoke-role.sh", log=output / ("smoke-" + role + ".log"))
            report["checks"]["smoke"] = "passed"
        if args.check:
            print("Running the " + args.check + " image check...", flush=True)
            report["checks"]["image_check"] = "running"
            save_json(output / "build-report.json", report)
            run(check_command(tags, args.check, output / "image-check"), log=output / "image-check.log")
            report["checks"]["image_check"] = "passed"
        report["status"] = "passed"
        print("Built five images; report: " + str(output / "build-report.json"), flush=True)
        print(json.dumps(tags, indent=2), flush=True)
    except BaseException as error:
        report["status"] = "failed"
        report["error"] = str(error)
        for check, state in report["checks"].items():
            if state in ("running", "pending"):
                report["checks"][check] = "failed" if state == "running" else "not_run"
        raise
    finally:
        if container_id:
            cleanup = subprocess.run(["docker", "rm", "-f", container_id], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if cleanup.returncode:
                report["assembly_container_cleanup_error"] = cleanup.stderr.strip()
        save_json(output / "build-report.json", report)


if __name__ == "__main__":
    try:
        main()
    except (BuildError, OSError, ValueError, KeyError) as error:
        print("Ceph role image build failed: " + str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Ceph role image build interrupted", file=sys.stderr)
        sys.exit(130)
