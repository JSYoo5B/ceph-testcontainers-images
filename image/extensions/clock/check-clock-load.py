"""Load check of the clock helper's interposer (docs/EXTENSIONS.md).

image/extension_check.py copies the interposer out of the helper into a
created container of a Ceph image, as a module places it into a daemon
container, and runs this script there on python3's standard input, as root,
with no network and no capability. The script only measures; the checker
judges the samples.

The script is the uninstrumented reference. It starts a child python3 once
without the interposer and then with LD_PRELOAD, FAKETIME_DONT_FAKE_MONOTONIC=1
and a timestamp file that the script owns, once with +0 and once with +3600.
FAKETIME is never set: libfaketime disables its timestamp file when FAKETIME is
present. Each child reads CLOCK_REALTIME and CLOCK_MONOTONIC through the public
clock_gettime and reports whether the interposer is mapped into it; the script
reads both clocks before and after each child. It prints one JSON object.
It changes nothing outside its own temporary directory.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

INTERPOSER = sys.argv[1]
CHILD = ("import json, sys, time\n"
         "realtime = time.clock_gettime(time.CLOCK_REALTIME)\n"
         "monotonic = time.clock_gettime(time.CLOCK_MONOTONIC)\n"
         "with open('/proc/self/maps') as maps:\n"
         "    loaded = any(line.split()[-1:] == [sys.argv[1]] for line in maps)\n"
         "print(json.dumps({'realtime': realtime, 'monotonic': monotonic, 'loaded': loaded}))\n")


def clocks():
    return {"realtime": time.clock_gettime(time.CLOCK_REALTIME),
            "monotonic": time.clock_gettime(time.CLOCK_MONOTONIC)}


def sample(name, directory, content):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("FAKETIME") and key != "LD_PRELOAD"}
    if content is not None:
        stamp = os.path.join(directory, "offset")
        with open(stamp, "w") as output:
            output.write(content + "\n")
        environment.update(LD_PRELOAD=INTERPOSER, FAKETIME_TIMESTAMP_FILE=stamp, FAKETIME_DONT_FAKE_MONOTONIC="1")
    result = {"name": name, "stamp": content, "before": clocks()}
    try:
        child = subprocess.run([sys.executable, "-I", "-c", CHILD, INTERPOSER], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, env=environment, timeout=30)
    except subprocess.TimeoutExpired:
        result.update(after=clocks(), error="timeout")
        return result
    result.update(after=clocks(), exit_code=child.returncode, stderr=child.stderr[-500:])
    try:
        result["child"] = json.loads(child.stdout)
    except ValueError:
        result["error"] = "no measurement: " + child.stdout[-200:]
    return result


with tempfile.TemporaryDirectory() as directory:
    samples = [sample("reference", directory, None), sample("+0", directory, "+0"),
               sample("+3600", directory, "+3600")]
print(json.dumps({"interposer": INTERPOSER, "python": sys.version.split()[0], "samples": samples}))
