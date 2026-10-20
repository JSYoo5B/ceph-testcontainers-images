"""Userspace application compatibility probes; executed inside control images."""
import contextlib
import errno
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

stage = "dependencies"
proof = {}
CHUNK = 1 << 20


def digest(data):
    return hashlib.sha256(data).hexdigest()


def header_updates(before, after, chunk=64 << 10):
    """Return only changed blocks within an already bounded header buffer."""
    if len(before) != len(after):
        raise AssertionError("header lengths differ")
    return [(offset, after[offset:offset + chunk]) for offset in range(0, len(before), chunk)
            if before[offset:offset + chunk] != after[offset:offset + chunk]]


def file_tail_hash(path, start):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        stream.seek(start)
        for data in iter(lambda: stream.read(CHUNK), b""):
            result.update(data)
    return result.hexdigest()


def image_tail_hash(image, start, size):
    result = hashlib.sha256()
    for offset in range(start, size, CHUNK):
        result.update(image.read(offset, min(CHUNK, size - offset)))
    return result.hexdigest()


@contextlib.contextmanager
def session(pool):
    import rados
    with rados.Rados(conffile="/etc/ceph/ceph.conf", conf={
            "rados_mon_op_timeout": "15", "rados_osd_op_timeout": "30", "rbd_cache": "false"}) as connection:
        with connection.open_ioctx(pool) as io:
            yield io


def denied(image, fmt, key):
    import rbd
    try:
        image.encryption_load(fmt, key)
    except rbd.Error as error:
        # Other errors (missing libraries, bad headers, I/O) are not key denial.
        assert isinstance(error, rbd.PermissionError) and error.errno == errno.EPERM, \
            "unexpected rejection type=%s errno=%s" % (type(error).__name__, getattr(error, "errno", None))
        return {"type": type(error).__name__, "errno": error.errno}
    raise AssertionError("passphrase unexpectedly accepted")


def encryption(pool):
    import rbd
    global stage, proof
    executable = shutil.which("cryptsetup")
    if not executable:
        raise FileNotFoundError("cryptsetup executable is required")
    proof = {"formats": []}
    size = 64 << 20
    payload = hashlib.sha256(b"application encrypted RBD fixture").digest() * ((2 << 20) // 32)
    offset = 1 << 20
    for label, fmt in (("LUKS1", rbd.RBD_ENCRYPTION_FORMAT_LUKS1),
                       ("LUKS2", rbd.RBD_ENCRYPTION_FORMAT_LUKS2)):
        name = label.lower()
        old_key, new_key = "fixture-old-" + label, "fixture-new-" + label
        row = {"format": label}
        created = False
        try:
            stage = label + ":format-write"
            with session(pool) as io:
                rbd.RBD().create(io, name, size, old_format=False,
                                 features=rbd.RBD_FEATURE_LAYERING | rbd.RBD_FEATURE_EXCLUSIVE_LOCK)
                created = True
                with rbd.Image(io, name) as image:
                    identity = image.id()
                    image.encryption_format(fmt, old_key, rbd.RBD_ENCRYPTION_ALGORITHM_AES256)
                with rbd.Image(io, name) as image:
                    image.encryption_load(fmt, old_key)
                    logical_size = image.size()
                    header_size = size - logical_size
                    assert 0 < header_size < size - offset - len(payload), "invalid encrypted data offset"
                    image.write(payload, offset)
                    image.flush()
            # Both Image and the entire Rados connection have been closed.
            stage = label + ":fresh-client-read"
            with session(pool) as io:
                with rbd.Image(io, name) as image:
                    image.encryption_load(fmt, old_key)
                    assert image.size() == logical_size
                    assert image.read(offset, len(payload)) == payload, "fresh client payload differs"
            with tempfile.TemporaryDirectory(prefix="tc-luks-") as temporary:
                root = Path(temporary)
                raw_path, old_path, new_path = root / "raw-rbd", root / "old-key", root / "new-key"
                for path, key in ((old_path, old_key), (new_path, new_key)):
                    path.touch(mode=0o600)
                    path.write_bytes(key.encode())
                stage = label + ":raw-export"
                with session(pool) as io:
                    with rbd.Image(io, name) as image:
                        assert image.id() == identity and image.size() == size
                        header_before = image.read(0, header_size)
                        assert header_before[:6] == b"LUKS\xba\xbe"
                        assert int.from_bytes(header_before[6:8], "big") == int(label[-1])
                        assert image.read(header_size + offset, len(payload)) != payload, "raw data is plaintext"
                        ciphertext_hash = image_tail_hash(image, header_size, size)
                        with raw_path.open("wb") as stream:
                            for position in range(0, size, CHUNK):
                                stream.write(image.read(position, min(CHUNK, size - position)))
                stage = label + ":external-passphrase-change"
                subprocess.run([executable, "--batch-mode", "luksChangeKey", "--key-file", str(old_path),
                                "--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000",
                                str(raw_path), str(new_path)], check=True, capture_output=True, timeout=120)
                stage = label + ":exported-ciphertext-unchanged"
                assert raw_path.stat().st_size == size, "export size changed"
                assert file_tail_hash(raw_path, header_size) == ciphertext_hash, "external tool changed ciphertext"
                with raw_path.open("rb") as stream:
                    header_after = stream.read(header_size)
                updates = header_updates(header_before, header_after)
                assert updates, "external tool did not change the header"
                stage = label + ":apply-header-only"
                with session(pool) as io:
                    with rbd.Image(io, name) as image:
                        for position, data in updates:
                            assert position + len(data) <= header_size
                            image.write(data, position)
                        image.flush()
                        assert image.id() == identity and image.size() == size
                        assert image.read(0, header_size) == header_after, "applied header differs"
                        assert image_tail_hash(image, header_size, size) == ciphertext_hash, "RBD ciphertext changed"
            stage = label + ":old-passphrase-denied"
            with session(pool) as io:
                with rbd.Image(io, name) as image:
                    rejection = denied(image, fmt, old_key)
            stage = label + ":new-passphrase-read"
            with session(pool) as io:
                with rbd.Image(io, name) as image:
                    assert image.id() == identity
                    image.encryption_load(fmt, new_key)
                    assert image.size() == logical_size
                    assert image.read(offset, len(payload)) == payload, "data lost after header update"
            row.update(image_id=identity, raw_size=size, logical_size=logical_size, header_size=header_size,
                       changed_header_bytes=sum(a != b for a, b in zip(header_before, header_after)),
                       written_header_bytes=sum(len(data) for _, data in updates),
                       ciphertext_sha256=ciphertext_hash, payload_sha256=digest(payload),
                       old_passphrase_denial=rejection, fresh_client_read=True, new_passphrase_read=True,
                       identity_and_sizes_preserved=True, outside_header_ciphertext_unchanged=True)
            proof["formats"].append(row)
        finally:
            if created:
                # Preserve the failure stage when cleanup succeeds.
                with session(pool) as io:
                    rbd.RBD().remove(io, name)
                    assert name not in rbd.RBD().list(io), "encrypted image cleanup failed"
    stage = "cleanup"
    with session(pool) as io:
        assert not rbd.RBD().list(io), "RBD fixture images remain"
    proof["cleanup"] = {"status": "passed", "remaining_images": []}
    return proof


def object_class(pool):
    import rados
    global stage, proof
    with session(pool) as io:
        name = "hello-fixture"
        try:
            stage = "compound-record-hello"
            with rados.WriteOpCtx() as operation:
                operation.execute("hello", "record_hello", b"application")
                io.operate_write_op(operation, name)
            stage = "hello-replay"
            code, data = io.execute(name, "hello", "replay", b"", 4096)
            expected = b"Hello, application!"
            # Python Ioctx.execute returns the output byte count (the class
            # method's successful C return is converted by librados).
            assert code == len(expected), "hello.replay return byte count differs"
            assert data == expected, "hello.replay data differs"
            assert io.read(name, len(expected) + 1) == expected, "stored class data differs"
            proof = {"compound_write": True, "class": "hello", "replay_return": code,
                     "replay_hex": data.hex(), "stored_sha256": digest(expected)}
        finally:
            for obj in io.list_objects():
                obj.remove()
        stage = "cleanup"
        assert not list(io.list_objects()), "object-class fixtures remain"
    proof["cleanup"] = {"status": "passed", "remaining_objects": []}
    return proof


def striper(pool):
    import rados
    global stage, proof
    # The default striper object size is 4 MiB; 9 MiB must span >=3 shards.
    data = hashlib.sha256(b"striped application fixture").digest() * ((9 << 20) // 32)
    name = "striped-fixture"
    command = ["rados", "--striper", "-p", pool]
    with session(pool) as io:
        try:
            stage = "lock-class-lock-unlock"
            io.write_full("lock-fixture", b"lock probe")
            io.lock_exclusive("lock-fixture", "compatibility", "first")
            try:
                io.lock_exclusive("lock-fixture", "compatibility", "second")
            except rados.ObjectBusy:
                pass
            else:
                raise AssertionError("lock conflict was not rejected")
            io.unlock("lock-fixture", "compatibility", "first")
            io.lock_exclusive("lock-fixture", "compatibility", "second")
            io.unlock("lock-fixture", "compatibility", "second")
            io.remove_object("lock-fixture")
            with tempfile.TemporaryDirectory(prefix="tc-striper-") as temporary:
                source, target = Path(temporary) / "source", Path(temporary) / "target"
                source.write_bytes(data)
                stage = "striper-put"
                subprocess.run(command + ["put", name, str(source)], check=True, capture_output=True, timeout=120)
                stage = "striper-shards"
                shards = sorted(obj.key for obj in io.list_objects())
                assert len(shards) >= 3 and all(key.startswith(name + ".") for key in shards), \
                    "striper did not create multiple shards"
                stage = "striper-get"
                subprocess.run(command + ["get", name, str(target)], check=True, capture_output=True, timeout=120)
                assert target.read_bytes() == data, "striped round trip differs"
                stage = "striper-rm"
                subprocess.run(command + ["rm", name], check=True, capture_output=True, timeout=120)
                assert not list(io.list_objects()), "striper rm left objects"
            proof = {"bytes": len(data), "default_object_size": 4 << 20, "shards": shards,
                     "payload_sha256": digest(data), "exact_round_trip": True,
                     "lock_conflict_denied": True, "lock_unlock_and_reacquire": True,
                     "cleanup": {"status": "passed", "remaining_objects": []}}
        finally:
            for obj in io.list_objects():
                obj.remove()
    return proof


def main():
    global stage
    try:
        result = {"rbd-encryption": encryption, "rados-object-class": object_class,
                  "rados-striper": striper}[sys.argv[1]](sys.argv[2])
        print("PROBE_RESULT\t" + json.dumps(result, sort_keys=True), flush=True)
    except Exception as error:
        # Keys and subprocess output are deliberately excluded from reports.
        kind = "missing_file" if isinstance(error, (FileNotFoundError, ModuleNotFoundError)) else \
            "loading_failure" if isinstance(error, ImportError) else "functional_failure"
        print("PROBE_FAILURE\t" + json.dumps({"failure_stage": stage, "failure_kind": kind,
              "error_type": type(error).__name__, "errno": getattr(error, "errno", None),
              "detail": str(error) if isinstance(error, (AssertionError, FileNotFoundError)) else None,
              "proof": proof}, sort_keys=True), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
