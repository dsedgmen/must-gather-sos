import unittest

from must_gather_sos import UserError
from must_gather_sos.catalog import (CORE, Catalog, build_selection,
                                     parse_options, resolve, split_list)


class SelectionTestCase(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()

    def select(self, **kwargs):
        return build_selection(self.catalog, **kwargs)


class TestSplitList(SelectionTestCase):
    def test_forms(self):
        self.assertEqual(split_list("a,b"), ["a", "b"])
        self.assertEqual(split_list("a b"), ["a", "b"])
        self.assertEqual(split_list("a, b ,c"), ["a", "b", "c"])
        self.assertEqual(split_list(""), [])
        self.assertEqual(split_list(None), [])


class TestResolve(SelectionTestCase):
    def test_default_is_the_default_set(self):
        names, _ = resolve(self.catalog)
        self.assertEqual(set(names), self.catalog.defaults)
        self.assertIn(CORE, names)

    def test_skip_removes(self):
        names, _ = resolve(self.catalog, skip="monitoring,istio")
        self.assertNotIn("monitoring", names)
        self.assertNotIn("istio", names)
        self.assertIn("etcd", names)

    def test_enable_adds_non_default(self):
        names, _ = resolve(self.catalog, enable="audit_logs")
        self.assertIn("audit_logs", names)
        self.assertTrue(self.catalog.defaults.issubset(set(names)))

    def test_only_is_exact(self):
        names, _ = resolve(self.catalog, only="etcd,sriov")
        self.assertEqual(names, sorted(["etcd", "sriov"], key=self.catalog.sort_key))
        self.assertNotIn(CORE, names)

    def test_only_can_include_core(self):
        names, _ = resolve(self.catalog, only="core,etcd")
        self.assertEqual(names[0], CORE)

    def test_only_rejects_skip_and_enable(self):
        with self.assertRaises(UserError):
            resolve(self.catalog, only="etcd", skip="monitoring")
        with self.assertRaises(UserError):
            resolve(self.catalog, only="etcd", enable="audit_logs")

    def test_all_and_only_are_exclusive(self):
        with self.assertRaises(UserError):
            resolve(self.catalog, only="etcd", all_plugins=True)

    def test_unknown_name_anywhere_is_an_error(self):
        for kwargs in ({"only": "nope"}, {"skip": "nope"}, {"enable": "nope"}):
            with self.assertRaises(UserError):
                resolve(self.catalog, **kwargs)

    def test_skip_everything_is_an_error(self):
        with self.assertRaises(UserError):
            resolve(self.catalog, skip=",".join(self.catalog.defaults))

    def test_superset_with_skip_is_an_error(self):
        with self.assertRaises(UserError) as ctx:
            resolve(self.catalog, enable="network_logs", skip="istio")
        self.assertIn("network_logs", str(ctx.exception))

    def test_superset_alone_is_fine(self):
        names, warnings = resolve(self.catalog, only="network_logs")
        self.assertEqual(names, ["network_logs"])
        self.assertEqual(warnings, [])

    def test_superset_with_others_warns(self):
        _, warnings = resolve(self.catalog, only="network_logs,etcd")
        self.assertTrue(any("network_logs" in w for w in warnings))

    def test_all_plugins_omits_unrunnable_ones(self):
        names, warnings = resolve(self.catalog, all_plugins=True)
        # metrics refuses to run without an option; network_logs is a superset.
        self.assertNotIn("metrics", names)
        self.assertNotIn("network_logs", names)
        self.assertIn("audit_logs", names)
        self.assertTrue(warnings)

    def test_all_plugins_keeps_configured_option_plugin(self):
        names, _ = resolve(self.catalog, all_plugins=True, configured={"metrics"})
        self.assertIn("metrics", names)

    def test_order_follows_upstream_with_core_first(self):
        names, _ = resolve(self.catalog)
        self.assertEqual(names[0], CORE)
        self.assertEqual(names[-1], "aro")  # upstream runs it last, serially


class TestParseOptions(SelectionTestCase):
    def parse(self, *raw):
        return parse_options(self.catalog, list(raw))

    def test_bool_forms(self):
        for value in ("on", "true", "YES", "1"):
            self.assertTrue(self.parse(f"audit_logs.force={value}")["audit_logs"]["force"])
        for value in ("off", "false", "No", "0"):
            self.assertFalse(self.parse(f"audit_logs.force={value}")["audit_logs"]["force"])

    def test_bare_bool_means_on(self):
        self.assertTrue(self.parse("audit_logs.force")["audit_logs"]["force"])

    def test_bad_bool(self):
        with self.assertRaises(UserError):
            self.parse("audit_logs.force=maybe")

    def test_list_option_accumulates(self):
        parsed = self.parse("metrics.match=up", "metrics.match=down")
        self.assertEqual(parsed["metrics"]["match"], ["up", "down"])

    def test_repeatable_flag_value_is_taken_verbatim(self):
        # A Prometheus series selector contains commas and spaces; splitting on
        # them would silently turn one selector into several broken ones.
        selector = '{job="etcd", instance="a b"}'
        parsed = self.parse(f"metrics.match={selector}")
        self.assertEqual(parsed["metrics"]["match"], [selector])

    def test_positional_list_is_comma_split(self):
        parsed = self.parse("service_logs.services=crio,kubelet")
        self.assertEqual(parsed["service_logs"]["services"], ["crio", "kubelet"])

    def test_value_may_contain_equals(self):
        parsed = self.parse("metrics.min_time=a=b")
        self.assertEqual(parsed["metrics"]["min_time"], "a=b")

    def test_choices_enforced(self):
        self.assertEqual(
            self.parse("service_logs.roles=worker")["service_logs"]["roles"], "worker"
        )
        with self.assertRaises(UserError):
            self.parse("service_logs.roles=controlplane")

    def test_malformed(self):
        for raw in ("noplugin", "etcd.", "audit_logs.nope=1"):
            with self.assertRaises(UserError):
                self.parse(raw)

    def test_value_required_for_non_bool(self):
        with self.assertRaises(UserError):
            self.parse("insights.collector")

    def test_plugin_without_options(self):
        with self.assertRaises(UserError) as ctx:
            self.parse("etcd.anything=1")
        self.assertIn("takes no options", str(ctx.exception))

    def test_typo_suggestion(self):
        with self.assertRaises(UserError) as ctx:
            self.parse("audit_logs.forc=on")
        self.assertIn("force", str(ctx.exception))


class TestBuildSelection(SelectionTestCase):
    def test_option_for_unselected_plugin_rejected(self):
        with self.assertRaises(UserError) as ctx:
            self.select(only="etcd", raw_options=["audit_logs.force=on"])
        self.assertIn("audit_logs", str(ctx.exception))

    def test_requires_option_enforced(self):
        with self.assertRaises(UserError):
            self.select(only="metrics")
        selection = self.select(only="metrics", raw_options=["metrics.max_time=1"])
        self.assertEqual(selection.names, ["metrics"])


class TestInvocation(SelectionTestCase):
    def invocation(self, name, **kwargs):
        selection = self.select(**kwargs)
        return selection.invocation(self.catalog.get(name))

    def test_plain_plugin(self):
        env, argv = self.invocation("etcd", only="etcd")
        self.assertEqual(env, {})
        self.assertEqual(argv, ["/usr/bin/gather_etcd"])

    def test_upstream_default_args_preserved(self):
        _, argv = self.invocation("service_logs", only="service_logs")
        self.assertEqual(argv, ["/usr/bin/gather_service_logs", "master"])

    def test_positional_override(self):
        _, argv = self.invocation(
            "service_logs", only="service_logs",
            raw_options=["service_logs.roles=worker"],
        )
        self.assertEqual(argv, ["/usr/bin/gather_service_logs", "worker"])

    def test_second_positional_uses_separator(self):
        _, argv = self.invocation(
            "service_logs", only="service_logs",
            raw_options=["service_logs.roles=worker",
                         "service_logs.services=crio,kubelet"],
        )
        self.assertEqual(argv, ["/usr/bin/gather_service_logs", "worker", "crio kubelet"])

    def test_later_positional_keeps_upstream_default_for_earlier_one(self):
        # services is index 1; roles (index 0) must stay at upstream's 'master'
        # rather than becoming empty and shifting the script's arguments.
        _, argv = self.invocation("service_logs", only="service_logs",
                                  raw_options=["service_logs.services=crio"])
        self.assertEqual(argv, ["/usr/bin/gather_service_logs", "master", "crio"])

    def test_positional_gap_is_rejected(self):
        # Synthetic plugin with no default args: setting index 1 alone leaves a
        # hole at index 0, which would silently shift the arguments.
        plugin = self.catalog.get("service_logs")
        plugin.args = []
        selection = self.select(only="service_logs",
                                raw_options=["service_logs.services=crio"])
        with self.assertRaises(UserError) as ctx:
            selection.invocation(plugin)
        self.assertIn("positional argument 0", str(ctx.exception))

    def test_flag_option(self):
        _, argv = self.invocation("audit_logs", only="audit_logs",
                                  raw_options=["audit_logs.force=on"])
        self.assertEqual(argv, ["/usr/bin/gather_audit_logs", "--force"])

    def test_flag_option_off_omits_flag(self):
        _, argv = self.invocation("audit_logs", only="audit_logs",
                                  raw_options=["audit_logs.force=off"])
        self.assertEqual(argv, ["/usr/bin/gather_audit_logs"])

    def test_repeatable_opt(self):
        _, argv = self.invocation(
            "metrics", only="metrics",
            raw_options=["metrics.match=up", "metrics.match=down",
                         "metrics.min_time=5"],
        )
        self.assertEqual(argv[1:], ["--match=up", "--match=down", "--min-time=5"])

    def test_env_option(self):
        env, argv = self.invocation("profiling_node", only="profiling_node",
                                    raw_options=["profiling_node.seconds=60"])
        self.assertEqual(env, {"PROFILING_NODE_SECONDS": "60"})
        self.assertEqual(argv, ["/usr/bin/gather_profiling_node"])

    def test_env_if_core_applied_only_with_core(self):
        env, _ = self.invocation("ppc", only="core,ppc")
        self.assertEqual(env, {"MUST_GATHER_NODES_INSPECTED": "true"})
        env, _ = self.invocation("ppc", only="ppc")
        self.assertEqual(env, {}, "ppc must inspect nodes itself when core is skipped")


if __name__ == "__main__":
    unittest.main()
