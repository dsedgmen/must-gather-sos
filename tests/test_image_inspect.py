"""Tests for experimental must-gather image inspect / catalog generation."""

from __future__ import annotations

import json
import os
import unittest

from must_gather_sos import image_inspect
from must_gather_sos.catalog import Catalog

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "data", "cluster_catalog")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


class TestParseInspectOutput(unittest.TestCase):
    def test_cnv_fixture(self):
        parsed = image_inspect.parse_inspect_output(_load("inspect-cnv.log"))
        self.assertIn("common.sh", parsed["libs"])
        self.assertIn("cdi", parsed["scripts"])
        self.assertIn("etcd", parsed["scripts"])
        self.assertNotIn("service_logs_util", parsed["scripts"])
        self.assertIn("mandatory_scripts", parsed["gather"])

    def test_openstack_fixture(self):
        parsed = image_inspect.parse_inspect_output(
            _load("inspect-openstack.log")
        )
        self.assertEqual(
            sorted(parsed["scripts"]),
            ["ctlplane_resources", "edpm_sos", "sos"],
        )


class TestBuildCatalogExperimental(unittest.TestCase):
    def test_cnv_defaults_from_mandatory_scripts_only(self):
        inspected = image_inspect.parse_inspect_output(_load("inspect-cnv.log"))
        image = (
            "registry.redhat.io/container-native-virtualization/"
            "cnv-must-gather-rhel9@sha256:bbb"
        )
        doc = image_inspect.build_catalog_from_inspect(image, inspected)
        Catalog(doc, path="<test>")  # must load
        self.assertEqual(doc["name"], "cnv-must-gather")
        self.assertEqual(doc["source"]["method"], "cluster-pod-experimental")
        self.assertEqual(doc["image"]["default"], image)
        self.assertTrue(doc["plugins"]["cdi"]["default"])
        self.assertTrue(doc["plugins"]["hco"]["default"])
        # OpenShift leftover in the image is listed but not default.
        self.assertFalse(doc["plugins"]["etcd"]["default"])
        # Optional flag-only script.
        self.assertFalse(doc["plugins"]["images"]["default"])
        self.assertFalse(doc["plugins"]["vms_details"]["default"])
        # CNV entrypoint is not openshift-layout core.
        self.assertNotIn("core", doc)

    def test_openstack_all_non_default_without_invocations(self):
        inspected = image_inspect.parse_inspect_output(
            _load("inspect-openstack.log")
        )
        image = (
            "registry.redhat.io/rhoso-operators/"
            "openstack-must-gather-rhel9@sha256:ddd"
        )
        doc = image_inspect.build_catalog_from_inspect(image, inspected)
        Catalog(doc, path="<test>")
        self.assertEqual(doc["name"], "openstack-must-gather")
        self.assertTrue(all(not p["default"] for p in doc["plugins"].values()))
        self.assertIn("ctlplane_resources", doc["plugins"])
        self.assertEqual(doc["profile"]["id"], "openshift")

    def test_mandatory_scripts_parser(self):
        names = image_inspect.parse_mandatory_scripts(
            'declare mandatory_scripts=(\n    "a"\n    "b"\n  )\n'
        )
        self.assertEqual(names, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
