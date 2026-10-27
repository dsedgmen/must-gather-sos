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


DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "must_gather_sos", "data",
)


def shipped_catalogs():
    """Every catalog we ship, by name."""
    return {
        os.path.splitext(f)[0]: os.path.join(DATA_DIR, f)
        for f in sorted(os.listdir(DATA_DIR)) if f.endswith(".json")
    }


class TestEveryShippedCatalog(unittest.TestCase):
    """Structural invariants that hold for any catalog, not just OpenShift's.

    Separate from the OpenShift content assertions below so that adding a
    catalog for another product picks these up without inheriting claims
    about etcd and clusterversion.
    """

    def test_all_load_and_are_self_describing(self):
        for name, path in shipped_catalogs().items():
            with self.subTest(catalog=name):
                catalog = Catalog.load(path)
                self.assertIn(catalog.version, (1, 2))
                self.assertTrue(catalog.source.get("commit"))
                self.assertEqual(catalog.name, name)

    def test_every_plugin_has_a_script_and_a_description(self):
        for name, path in shipped_catalogs().items():
            catalog = Catalog.load(path)
            for plugin_name, plugin in catalog.plugins.items():
                with self.subTest(catalog=name, plugin=plugin_name):
                    if plugin.is_core:
                        self.assertIsNone(plugin.script)
                        continue
                    self.assertTrue(plugin.script)
                    # Either an executable in the image, or the name of a
                    # shell function its library defines -- ACM collects
                    # hosted clusters with dump_hostedcluster.
                    if plugin.script.startswith("/"):
                        self.assertTrue(plugin.script.startswith("/usr/bin/"))
                    else:
                        self.assertRegex(plugin.script, r"^[A-Za-z_][A-Za-z0-9_]*$")
                    self.assertTrue(plugin.description)

    def test_phases_and_option_specs_are_well_formed_everywhere(self):
        for name, path in shipped_catalogs().items():
            catalog = Catalog.load(path)
            for plugin_name, plugin in catalog.plugins.items():
                with self.subTest(catalog=name, plugin=plugin_name):
                    self.assertIn(plugin.phase, VALID_PHASES)
                    for opt, spec in plugin.options.items():
                        where = f"{plugin_name}.{opt}"
                        self.assertIn(spec.get("kind", "opt"), VALID_KINDS, where)
                        self.assertIn(spec.get("type", "str"), VALID_TYPES, where)

    def test_profiles_are_usable(self):
        for name, path in shipped_catalogs().items():
            with self.subTest(catalog=name):
                profile = Catalog.load(path).profile
                self.assertTrue(profile.id)
                self.assertTrue(profile.detect)
                self.assertTrue(profile.base_path.startswith("/"))
                self.assertTrue(profile.source)

    def test_exactly_one_catalog_claims_the_payload_image(self):
        # It is the fallback when no --image is given, so two would be
        # ambiguous and none would leave the default run with no catalog.
        claimants = [n for n, p in shipped_catalogs().items()
                     if Catalog.load(p).image.payload]
        self.assertEqual(claimants, ["openshift"])

    def test_image_patterns_do_not_overlap_between_catalogs(self):
        # Matching is by substring, so "must-gather" in one catalog would
        # also claim another product's "acm-must-gather".
        catalogs = {n: Catalog.load(p) for n, p in shipped_catalogs().items()}
        for name, catalog in catalogs.items():
            for pattern in catalog.image.match:
                for other_name, other in catalogs.items():
                    if other_name == name:
                        continue
                    with self.subTest(pattern=pattern, other=other_name):
                        self.assertFalse(
                            other.image.matches(f"registry.example.com/{pattern}:v1"),
                            f"{other_name} also claims {name}'s {pattern!r}",
                        )


class TestShippedCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def test_loads(self):
        self.assertEqual(self.catalog.version, 2)
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

    def test_every_real_plugin_follows_the_upstream_script_naming(self):
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

    def _doc(self, **changes):
        with open(CATALOG_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc.update(changes)
        return doc

    def test_a_version_1_catalog_still_loads_with_the_default_profile(self):
        # v1 documents have no profile key at all; they predate the idea.
        doc = self._doc(catalog_version=1)
        doc.pop("profile", None)
        doc.pop("image", None)
        catalog = Catalog(doc, "test")
        self.assertEqual(catalog.profile.id, "openshift")
        self.assertEqual(catalog.profile.source, ["/usr/bin/common.sh"])
        self.assertIn(CORE, catalog.names)

    def test_core_is_optional(self):
        doc = self._doc()
        doc.pop("core")
        catalog = Catalog(doc, "test")
        self.assertNotIn(CORE, catalog.names)
        self.assertIsNone(catalog.core_resources)

    def test_core_without_common_sh_is_rejected(self):
        # The core template expands ${log_collection_args}, which only
        # common.sh defines; an unfiltered inspect would be silent breakage.
        doc = self._doc()
        doc["profile"] = dict(doc["profile"], setup=["check_if_hub"])
        with self.assertRaises(UserError) as ctx:
            Catalog(doc, "test")
        self.assertIn("get_log_collection_args", str(ctx.exception))

    def test_an_explicitly_null_version_command_is_kept(self):
        # Distinct from an absent key: it means the image has no
        # /usr/bin/version, not "use the usual one".
        doc = self._doc()
        doc["profile"] = dict(doc["profile"], version={"banner": "x",
                                                       "command": None})
        self.assertIsNone(Catalog(doc, "test").profile.version_command)


class TestImageMatching(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def test_matches_regardless_of_registry_and_tag(self):
        for ref in (
            "quay.io/openshift/origin-must-gather:4.18",
            "registry.redhat.io/openshift4/ose-must-gather:v4.18",
            "mirror.example.com:5000/openshift/origin-must-gather@sha256:abc",
            "openshift/origin-must-gather",
        ):
            with self.subTest(ref=ref):
                self.assertTrue(self.catalog.image.matches(ref))

    def test_does_not_claim_another_products_must_gather(self):
        self.assertFalse(self.catalog.image.matches(
            "registry.redhat.io/rhacm2/acm-must-gather-rhel9:v2.14"))

    def test_no_reference_matches_nothing(self):
        self.assertFalse(self.catalog.image.matches(None))
        self.assertFalse(self.catalog.image.matches(""))


if __name__ == "__main__":
    unittest.main()


class TestSinceClassification(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def test_every_plugin_is_classified(self):
        for name, plugin in self.catalog.plugins.items():
            self.assertIn(plugin.since,
                          {"both", "pod-logs", "journal", "passed-through",
                           "none"}, name)

    def test_file_fetching_plugins_forward_since_but_it_has_no_effect(self):
        # These scripts really do pass the since arguments to oc adm
        # node-logs, so the plugin supports it -- but oc only attaches the
        # parameter when the path is 'journal' (openshift/oc
        # pkg/cli/admin/node/logs.go) and the kubelet does not time-filter
        # plain file serving, so whole files come back either way.
        for name in ("audit_logs", "kas_startup_termination_logs",
                     "windows_node_logs"):
            self.assertEqual(self.catalog.get(name).since, "passed-through",
                             name)
            self.assertFalse(self.catalog.get(name).honours_since, name)

    def test_plugins_that_never_mention_since(self):
        for name in ("etcd", "monitoring", "insights"):
            self.assertEqual(self.catalog.get(name).since, "none", name)

    def test_inspect_based_plugins_honour_since(self):
        for name in ("core", "sriov", "olm", "network_logs_basics"):
            self.assertEqual(self.catalog.get(name).since, "pod-logs", name)

    def test_service_logs_honours_since_via_journal(self):
        self.assertEqual(self.catalog.get("service_logs").since, "journal")

    def test_ppc_honours_since_both_ways(self):
        self.assertEqual(self.catalog.get("ppc").since, "both")


class TestForkCatalog(unittest.TestCase):
    """The shipped better-gather catalog, for runs against that image."""

    FORK = os.path.join(os.path.dirname(CATALOG_PATH), "better-gather.json")

    def setUp(self):
        if not os.path.exists(self.FORK):
            self.skipTest("better-gather catalog not shipped")
        self.upstream = Catalog.load()
        self.fork = Catalog.load(self.FORK)

    def test_same_plugin_set_as_upstream(self):
        self.assertEqual(set(self.fork.names), set(self.upstream.names))

    def test_fork_applies_the_window_to_file_collection(self):
        # These are exactly the plugins better-gather teaches to honour the
        # window; upstream cannot, because oc never passes it through.
        for name in ("audit_logs", "kas_startup_termination_logs",
                     "insights", "core_dumps",
                     "machineconfigdaemon_termination_logs"):
            with self.subTest(plugin=name):
                self.assertFalse(self.upstream.get(name).honours_since, name)
                self.assertEqual(self.fork.get(name).since, "files", name)
                self.assertTrue(self.fork.get(name).honours_since, name)

    def test_fork_never_reports_less_support_than_upstream(self):
        for name in self.upstream.names:
            if self.upstream.get(name).honours_since:
                self.assertTrue(self.fork.get(name).honours_since, name)

    def test_windows_node_logs_is_honestly_still_unsupported(self):
        # Fixed filenames, nothing for a window to select between.
        self.assertFalse(self.fork.get("windows_node_logs").honours_since)
