"""RBD probe run with the container's python3 and the Ceph rbd binding.

Usage:
  rbd_io.py create POOL IMAGE SIZE
  rbd_io.py write POOL IMAGE OFFSET SEED SIZE
  rbd_io.py sha POOL IMAGE [SNAPSHOT]
"""
import hashlib
import sys

import rados
import rbd


def payload(seed, size):
    """Deterministic bytes, so separate processes and clusters agree on content."""
    count = (size + 31) // 32
    return b"".join(hashlib.sha256(("%s:%d" % (seed, index)).encode()).digest() for index in range(count))[:size]


def main(argv):
    mode, pool, name = argv[1], argv[2], argv[3]
    cluster = rados.Rados(conffile="/etc/ceph/ceph.conf")
    cluster.connect()
    try:
        ioctx = cluster.open_ioctx(pool)
        try:
            if mode == "create":
                rbd.RBD().create(ioctx, name, int(argv[4]))
                print("ok")
            elif mode == "write":
                with rbd.Image(ioctx, name) as image:
                    data = payload(argv[5], int(argv[6]))
                    written = image.write(data, int(argv[4]))
                    if written != len(data):
                        raise SystemExit("short write")
                    image.flush()
                print("ok")
            elif mode == "sha":
                snapshot = argv[4] if len(argv) > 4 else None
                digest = hashlib.sha256()
                with rbd.Image(ioctx, name, snapshot=snapshot, read_only=True) as image:
                    size = image.size()
                    offset = 0
                    while offset < size:
                        chunk = image.read(offset, min(4 << 20, size - offset))
                        digest.update(chunk)
                        offset += len(chunk)
                print(digest.hexdigest())
            else:
                raise SystemExit("unknown mode " + mode)
        finally:
            ioctx.close()
    finally:
        cluster.shutdown()


if __name__ == "__main__":
    main(sys.argv)
