"""CephFS probe run with the container's python3 and the Ceph cephfs binding.

Usage:
  cephfs_io.py basic FILESYSTEM
  cephfs_io.py write FILESYSTEM PATH SEED SIZE
  cephfs_io.py read FILESYSTEM PATH SEED SIZE
  cephfs_io.py mkdir FILESYSTEM PATH
  cephfs_io.py isdir FILESYSTEM PATH
"""
import errno
import hashlib
import os
import stat
import sys

import cephfs


def payload(seed, size):
    """Deterministic bytes, so separate processes and clusters agree on content."""
    count = (size + 31) // 32
    return b"".join(hashlib.sha256(("%s:%d" % (seed, index)).encode()).digest() for index in range(count))[:size]


def mount(filesystem):
    fs = cephfs.LibCephFS(conffile="/etc/ceph/ceph.conf")
    fs.mount(filesystem_name=filesystem)
    return fs


def close(fs):
    fs.unmount()
    fs.shutdown()


def write(fs, path, data):
    fs.mkdirs(os.path.dirname(path) or "/", 0o755)
    fd = fs.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        if fs.write(fd, data, 0) != len(data):
            raise SystemExit("short write")
        fs.fsync(fd, 0)
    finally:
        fs.close(fd)


def read(fs, path):
    size = fs.stat(path).st_size
    fd = fs.open(path, os.O_RDONLY, 0)
    try:
        return fs.read(fd, 0, size)
    finally:
        fs.close(fd)


def missing(fs, path):
    try:
        fs.stat(path)
    except cephfs.ObjectNotFound:
        return True
    except cephfs.Error as error:
        return getattr(error, "errno", None) == errno.ENOENT
    return False


def basic(filesystem):
    data = payload("cephfs", 1 << 20)
    fs = mount(filesystem)
    try:
        write(fs, "/tc-basic/first", data)
        fs.rename("/tc-basic/first", "/tc-basic/second")
        if not missing(fs, "/tc-basic/first"):
            raise SystemExit("renamed source still exists")
    finally:
        close(fs)
    fs = mount(filesystem)  # A new session must see the persisted file.
    try:
        if read(fs, "/tc-basic/second") != data:
            raise SystemExit("file bytes differ in a new session")
        fs.unlink("/tc-basic/second")
        if not missing(fs, "/tc-basic/second"):
            raise SystemExit("unlinked file still exists")
        fs.rmdir("/tc-basic")
    finally:
        close(fs)


def main(argv):
    mode, filesystem = argv[1], argv[2]
    if mode == "basic":
        basic(filesystem)
    else:
        fs = mount(filesystem)
        try:
            path = argv[3]
            if mode == "write":
                write(fs, path, payload(argv[4], int(argv[5])))
            elif mode == "read":
                if read(fs, path) != payload(argv[4], int(argv[5])):
                    raise SystemExit("file bytes differ")
            elif mode == "mkdir":
                fs.mkdir(path, 0o755)  # Single level, so it also creates .snap entries.
            elif mode == "isdir":
                if not stat.S_ISDIR(fs.stat(path).st_mode):
                    raise SystemExit("not a directory")
            else:
                raise SystemExit("unknown mode " + mode)
        finally:
            close(fs)
    print("ok")


if __name__ == "__main__":
    main(sys.argv)
