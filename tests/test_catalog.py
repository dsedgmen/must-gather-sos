import json
import os
import unittest

from must_gather_sos import UserError
from must_gather_sos.catalog import CORE, Catalog

CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "must_gather_sos", "data", "openshift.json",
)

VALID_KINDS = {"flag", "opt", "positional", "env"}
VALID_TYPES = {"bool", "str", "list"}
VALID_PHASES = {"core", "parallel", "post"}


class TestShippedCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def test_loads(self):
        self.assertEqual(self.catalog.version, 1)
        self.assertTrue(self.catalog.source.get("commit"))

    def test_core_is_synthetic_and_default(self):
        core = self.catalog.get(CORE)
        self.assertTrue(core.default)
        self.assertEqual(core.phase, "core")
        self.assertIsNone(core.script)

    def test_core_resource_lists_are_populated(self):
        core = self.catalog.core_resources
        for key in ("named_resources", "group_resources", "all_ns_resources"):
            self.assertTrue(core[key], f"{key} is empty")
        self.assertIn("ns/openshift-cluster-version", core["named_resources"])
        self.assertIn("clusterversion", core["group_resources"])

    def test_known_plugins_present(self):
        for name in ("etcd", "audit_logs", "sriov", "monitoring", "aro"):
            self.assertIn(name, self.catalog.names)

    def test_internal_scripts_excluded(self):
        # Library, and a helper gather_ppc calls itself.
        self.assertNotIn("service_logs_util", self.catalog.names)
        self.assertNotIn("ppc_single_node", self.catalog.names)

    def test_every_real_plugin_has_a_script_and_description(self):
        for name, plugin in self.catalog.plugins.items():
            if plugin.is_core:
                continue
            self.assertEqual(plugin.script, f"/usr/bin/gather_{name}")
            self.assertTrue(plugin.description, f"{name} has no description")

    def test_phases_and_option_specs_are_well_formed(self):
        for name, plugin in self.catalog.plugins.items():
            self.assertIn(plugin.phase, VALID_PHASES, name)
            for opt, spec in plugin.options.items():
                where = f"{name}.{opt}"
                self.assertIn(spec.get("kind", "opt"), VALID_KINDS, where)
                self.assertIn(spec.get("type", "str"), VALID_TYPES, where)
                kind = spec.get("kind", "opt")
                if kind in ("flag", "opt"):
                    self.assertTrue(spec.get("flag"), where)
                elif kind == "positional":
                    self.assertIsInstance(spec.get("index"), int, where)
                elif kind == "env":
                    self.assertTrue(spec.get("env"), where)

    def test_upstream_quirks_are_captured(self):
        # gather_aro runs after the parallel wait in upstream's gather.
        self.assertEqual(self.catalog.get("aro").phase, "post")
        # gather_ppc only gets MUST_GATHER_NODES_INSPECTED because the base
        # inspect already covered nodes.
        self.assertEqual(
            self.catalog.get("ppc").env_if_core,
            {"MUST_GATHER_NODES_INSPECTED": "true"},
        )
        # gather_network_logs starts by running /usr/bin/gather.
        self.assertTrue(self.catalog.get("network_logs").superset)
        # gather_service_logs is invoked as `gather_service_logs master`.
        self.assertEqual(self.catalog.get("service_logs").args, ["master"])
        # gather_metrics exits 1 when called with no arguments.
        self.assertTrue(self.catalog.get("metrics").requires_option)

    def test_default_set_matches_upstream_count(self):
        defaults = self.catalog.defaults
        self.assertIn(CORE, defaults)
        self.assertNotIn("audit_logs", defaults)
        self.assertNotIn("metrics", defaults)
        # core + the 25 scripts upstream's gather invokes.
        self.assertEqual(len(defaults), 26)

    def test_unknown_plugin_suggests_alternatives(self):
        with self.assertRaises(UserError) as ctx:
            self.catalog.get("audit_log")
        self.assertIn("audit_logs", str(ctx.exception))


class TestCatalogLoading(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaises(UserError):
            Catalog.load("/nonexistent/catalog.json")

    def test_bad_version_rejected(self):
        with open(CATALOG_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["catalog_version"] = 99
        with self.assertRaises(UserError):
            Catalog(doc, "test")

    def test_reserved_name_rejected(self):
        with open(CATALOG_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["plugins"]["core"] = {"script": "/usr/bin/gather_core"}
        with self.assertRaises(UserError):
            Catalog(doc, "test")


if __name__ == "__main__":
    unittest.main()
