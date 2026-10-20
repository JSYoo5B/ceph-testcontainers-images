import datetime
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import run  # noqa: E402
import suite  # noqa: E402


def probe(name):
    spec = importlib.util.spec_from_file_location("probe_" + name, HERE / "probes" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SignatureTests(unittest.TestCase):
    def test_matches_the_published_sigv4_get_object_example(self):
        # AWS S3 SigV4 documentation, "Example: GET Object".
        s3 = probe("s3")
        headers = {
            "host": "examplebucket.s3.amazonaws.com", "range": "bytes=0-9",
            "x-amz-content-sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "x-amz-date": "20130524T000000Z",
        }
        value = s3.authorization("GET", "/test.txt", "", headers, headers["x-amz-content-sha256"],
                                 "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                                 datetime.datetime(2013, 5, 24))
        self.assertTrue(value.endswith(
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))
        self.assertIn("SignedHeaders=host;range;x-amz-content-sha256;x-amz-date", value)


class PayloadTests(unittest.TestCase):
    def test_probes_generate_identical_deterministic_bytes(self):
        # rbd_io and cephfs_io import Ceph bindings, so compare their source.
        source = (HERE / "probes" / "s3.py").read_text()
        start = source.index("def payload")
        definition = source[start:source.index("\n\n\n", start)]
        for name in ("rbd_io", "cephfs_io"):
            self.assertIn(definition, (HERE / "probes" / (name + ".py")).read_text())
        data = probe("s3").payload("seed", 100)
        self.assertEqual(len(data), 100)
        self.assertEqual(data, probe("s3").payload("seed", 100))
        self.assertNotEqual(data, probe("s3").payload("other", 100))


class RunnerTests(unittest.TestCase):
    def test_all_image_fills_unset_roles(self):
        self.assertEqual(run.images_from(["all=a", "osd=b"]),
                         {"control": "a", "osd": "b", "rgw": "a", "mds": "a"})

    def test_missing_roles_and_unknown_scenarios_fail_before_docker(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                suite.run({"control": "a"}, ["rbd"], Path(temporary))
            with self.assertRaises(ValueError):
                suite.run({role: "a" for role in suite.ROLES}, ["unknown"], Path(temporary))

    def test_full_check_covers_single_and_multi_cluster_scenarios(self):
        self.assertEqual(set(suite.SCENARIOS), set(suite.SINGLE_FUNCTIONS) | set(suite.MULTI_FUNCTIONS))
        self.assertEqual(len(suite.MULTI_CLUSTER), 4)

    def test_cleanup_engine_failure_is_recorded_without_hiding_bootstrap_failure(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(suite, "Docker") as docker, \
                mock.patch.object(suite, "Cluster") as cluster:
            cluster.return_value.start.side_effect = suite.ClusterError("bootstrap failure")
            docker.return_value.cleanup.side_effect = suite.ClusterError("cleanup engine failure")
            docker.return_value.leftovers.return_value = []
            result = suite.run({role: "image" for role in suite.ROLES}, ["rbd-encryption"],
                               Path(temporary), log=lambda _: None)
        self.assertEqual(result["rbd-encryption"]["failure_stage"], "bootstrap")
        self.assertEqual(result["cleanup"]["status"], "failed")
        self.assertEqual(result["cleanup"]["failure_kind"], "cleanup_failure")
        self.assertTrue(result["cleanup"]["errors"])


class HeaderIsolationTests(unittest.TestCase):
    def test_updates_never_extend_past_header_including_short_last_block(self):
        client = probe("client_capabilities")
        before = b"abcdefghij"
        after = b"AbcdefghiJ"
        raw_ciphertext = b"ciphertext must stay byte identical"
        raw = bytearray(before + raw_ciphertext)
        updates = client.header_updates(before, after, chunk=4)
        self.assertTrue(updates)
        for offset, data in updates:
            self.assertLessEqual(offset + len(data), len(before))
            raw[offset:offset + len(data)] = data
        self.assertEqual(raw, after + raw_ciphertext)
        self.assertEqual(client.header_updates(after, after), [])
        with self.assertRaises(AssertionError):
            client.header_updates(before, after + b"unexpected size change")


if __name__ == "__main__":
    unittest.main()
