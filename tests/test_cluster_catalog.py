"""Tests for cluster must-gather discovery and local catalog generation."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from unittest import mock

from must_gather_sos import cli, cluster_catalog
from must_gather_sos.catalog import Catalog

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "data", "cluster_catalog")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class TestDiscover(unittest.TestCase):
    def test_discovers_payload_annotation_and_related(self):
        csv = _load("csvs.json")
        is_doc = _load("imagestream-must-gather.json")

        def probe(oc, argv, timeout=30):
            joined = " ".join(argv)
            if "clusterversion" in joined:
                return "4.18.54"
            if "imagestream" in joined and "must-gather" in joined:
                return is_doc
            return None

        with mock.patch("must_gather_sos.runner.probe", side_effect=probe), \
             mock.patch("must_gather_sos.cluster_catalog._oc_json",
                        return_value=json.loads(csv)):
            entries, version = cluster_catalog.discover_must_gathers("oc")

        self.assertEqual(version, "4.18.54")
        images = [e["image"] for e in entries]
        self.assertEqual(
            images[0],
            "quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:payload",
        )
        self.assertIn(
            "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa", images
        )
        self.assertIn(
            "registry.redhat.io/container-native-virtualization/"
            "cnv-must-gather-rhel9@sha256:bbb",
            images,
        )
        self.assertIn(
            "registry.redhat.io/openshift4/"
            "ose-local-storage-mustgather-rhel9@sha256:ccc",
            images,
        )
        self.assertIn(
            "registry.redhat.io/rhoso-operators/"
            "openstack-must-gather-rhel9@sha256:ddd",
            images,
        )
        # ODF relatedImages must not duplicate the annotation entry.
        self.assertEqual(
            images.count(
                "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa"
            ),
            1,
        )
        by_image = {e["image"]: e for e in entries}
        self.assertEqual(
            by_image[
                "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa"
            ]["source"],
            "annotation",
        )
        self.assertEqual(
            by_image[
                "registry.redhat.io/rhoso-operators/"
                "openstack-must-gather-rhel9@sha256:ddd"
            ]["source"],
            "relatedImages",
        )


class TestMatchAndWrite(unittest.TestCase):
    def test_odf_matches_shipped(self):
        cat = cluster_catalog.match_shipped(
            "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa"
        )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.name, "odf")

    def test_cnv_does_not_match_shipped(self):
        cat = cluster_catalog.match_shipped(
            "registry.redhat.io/container-native-virtualization/"
            "cnv-must-gather-rhel9@sha256:bbb"
        )
        self.assertIsNone(cat)

    def test_payload_digest_uses_cluster_version(self):
        cat = cluster_catalog.match_shipped(
            "quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:payload",
            cluster_version="4.18.54",
            source="payload",
        )
        self.assertIsNotNone(cat)
        self.assertEqual(cat.name, "openshift-4.18")

    def test_write_shipped_sets_default_image(self):
        cat = Catalog.load("odf")
        with tempfile.TemporaryDirectory() as tmp:
            stem = cluster_catalog.write_shipped_catalog(
                cat,
                "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa",
                tmp,
            )
            self.assertEqual(stem, "odf")
            with open(os.path.join(tmp, "odf.json"), encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(
                doc["image"]["default"],
                "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa",
            )


class TestGenerateClusterCatalog(unittest.TestCase):
    def _entries(self):
        return [
            {
                "image": "quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:payload",
                "source": "payload",
                "from": "openshift/must-gather",
                "namespace": "openshift",
            },
            {
                "image": "registry.redhat.io/odf4/odf-must-gather-rhel9@sha256:aaa",
                "source": "annotation",
                "from": "openshift-storage/odf-operator.v4.18.0",
                "namespace": "openshift-storage",
            },
            {
                "image": (
                    "registry.redhat.io/container-native-virtualization/"
                    "cnv-must-gather-rhel9@sha256:bbb"
                ),
                "source": "annotation",
                "from": "openshift-cnv/kubevirt-hyperconverged-operator.v4.18.0",
                "namespace": "openshift-cnv",
            },
        ], "4.18.54"

    def test_default_writes_shipped_lists_unmatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with mock.patch("must_gather_sos.runner.which_oc", return_value="oc"), \
                 mock.patch("must_gather_sos.runner.check_cluster",
                            return_value="system:admin"), \
                 mock.patch("must_gather_sos.cluster_catalog.discover_must_gathers",
                            return_value=self._entries()):
                index = cluster_catalog.generate_cluster_catalog(
                    dest_dir=tmp, experimental_inspect=False, out=out,
                )
            names = {os.path.splitext(n)[0] for n in os.listdir(tmp)
                     if n.endswith(".json") and n != "index.json"}
            self.assertIn("openshift-4.18", names)
            self.assertIn("odf", names)
            self.assertNotIn("cnv-must-gather", names)
            by_gen = {r["generated"] for r in index["must_gathers"]}
            self.assertIn("shipped", by_gen)
            self.assertIn("unmatched", by_gen)
            self.assertIn("--experimental-inspect-catalogs", out.getvalue())

    def test_experimental_generates_unmatched(self):
        from must_gather_sos import image_inspect

        inspected = image_inspect.parse_inspect_output(
            _load("inspect-cnv.log")
        )

        def fake_inspect_and_write(image, namespace, dest_dir, oc="oc"):
            doc = image_inspect.build_catalog_from_inspect(image, inspected)
            path = os.path.join(dest_dir, f"{doc['name']}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
                fh.write("\n")
            return doc["name"]

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("must_gather_sos.runner.which_oc", return_value="oc"), \
                 mock.patch("must_gather_sos.runner.check_cluster",
                            return_value="system:admin"), \
                 mock.patch("must_gather_sos.cluster_catalog.discover_must_gathers",
                            return_value=self._entries()), \
                 mock.patch("must_gather_sos.image_inspect.inspect_and_write",
                            side_effect=fake_inspect_and_write):
                index = cluster_catalog.generate_cluster_catalog(
                    dest_dir=tmp, experimental_inspect=True, out=io.StringIO(),
                )
            self.assertTrue(
                os.path.exists(os.path.join(tmp, "cnv-must-gather.json"))
            )
            cnv = [r for r in index["must_gathers"]
                   if r["generated"] == "inspected-experimental"]
            self.assertEqual(len(cnv), 1)
            self.assertEqual(cnv[0]["catalog"], "cnv-must-gather")


class TestCliFlags(unittest.TestCase):
    def test_experimental_alone_errors(self):
        code, _, err = _run_cli("--experimental-inspect-catalogs")
        self.assertEqual(code, 1)
        self.assertIn("--generate-cluster-catalog", err)

    def test_generate_invokes_module(self):
        with mock.patch(
            "must_gather_sos.cluster_catalog.generate_cluster_catalog"
        ) as gen:
            code, _, err = _run_cli("--generate-cluster-catalog", "/tmp/cats")
            self.assertEqual(code, 0, err)
            gen.assert_called_once()
            kwargs = gen.call_args.kwargs
            self.assertEqual(kwargs["dest_dir"], "/tmp/cats")
            self.assertFalse(kwargs["experimental_inspect"])

    def test_generate_with_experimental(self):
        with mock.patch(
            "must_gather_sos.cluster_catalog.generate_cluster_catalog"
        ) as gen:
            code, _, _ = _run_cli(
                "--generate-cluster-catalog", "/tmp/cats",
                "--experimental-inspect-catalogs",
            )
            self.assertEqual(code, 0)
            self.assertTrue(gen.call_args.kwargs["experimental_inspect"])


def _run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main()
