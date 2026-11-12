"""Tests for the limited run-namespace helpers."""

import unittest
from unittest import mock

from must_gather_sos import UserError, namespace


class TestManifests(unittest.TestCase):
    def test_limit_range_yaml_uses_redhat_defaults(self):
        yaml = namespace.limit_range_yaml()
        self.assertIn("kind: LimitRange", yaml)
        self.assertIn(f"name: {namespace.LIMIT_RANGE_NAME}", yaml)
        self.assertIn("cpu: 500m", yaml)
        self.assertIn("memory: 2Gi", yaml)
        self.assertIn("cpu: 250m", yaml)
        self.assertIn("memory: 512Mi", yaml)

    def test_create_commands_are_pasteable(self):
        cmds = namespace.create_commands("mg")
        self.assertEqual(cmds[0], "oc create namespace mg")
        self.assertIn("system:serviceaccount:mg:default", cmds[1])
        self.assertTrue(cmds[2].startswith("oc apply -n mg -f - <<'EOF'"))
        self.assertTrue(cmds[2].endswith("EOF"))

    def test_cleanup_commands_drop_binding_then_namespace(self):
        cmds = namespace.cleanup_commands("mg")
        self.assertEqual(
            cmds,
            [
                "oc adm policy remove-cluster-role-from-user cluster-admin "
                "system:serviceaccount:mg:default",
                "oc delete namespace mg",
            ],
        )

    def test_format_print_commands_orders_create_gather_cleanup(self):
        text = namespace.format_print_commands(
            ["oc create namespace x"],
            "oc adm must-gather --run-namespace=x -- PAYLOAD",
            ["oc delete namespace x"],
        )
        create_at = text.index("# create")
        gather_at = text.index("# gather")
        cleanup_at = text.index("# clean up")
        self.assertLess(create_at, gather_at)
        self.assertLess(gather_at, cleanup_at)
        self.assertTrue(text.endswith("\n"))


class TestResolve(unittest.TestCase):
    def test_off_when_flag_absent(self):
        args = mock.Mock(limit_resources=False, run_namespace=None)
        self.assertIsNone(namespace.resolve_namespace(args))
        self.assertIsNone(namespace.apply_run_namespace(args))

    def test_default_name_when_flag_set(self):
        args = mock.Mock(limit_resources=True, run_namespace=None)
        self.assertEqual(namespace.apply_run_namespace(args),
                         namespace.DEFAULT_NAMESPACE)
        self.assertEqual(args.run_namespace, namespace.DEFAULT_NAMESPACE)

    def test_honours_explicit_run_namespace(self):
        args = mock.Mock(limit_resources=True, run_namespace="mine")
        self.assertEqual(namespace.apply_run_namespace(args), "mine")


class TestSetupCleanup(unittest.TestCase):
    def test_setup_creates_namespace_when_missing(self):
        with mock.patch("must_gather_sos.namespace.subprocess.run") as run:
            # get namespace -> missing; create; add role; apply
            run.side_effect = [
                mock.Mock(returncode=1, stdout="", stderr="not found"),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
            created = namespace.setup("oc", "mg")
        self.assertTrue(created)
        create = run.call_args_list[1][0][0]
        self.assertEqual(create[:3], ["oc", "create", "namespace"])
        self.assertEqual(create[3], "mg")
        apply = run.call_args_list[3]
        self.assertEqual(apply[0][0][:3], ["oc", "apply", "-n"])
        self.assertIn("kind: LimitRange", apply[1]["input"])

    def test_setup_reports_apply_failure(self):
        with mock.patch("must_gather_sos.namespace.subprocess.run") as run:
            run.side_effect = [
                mock.Mock(returncode=0, stdout="", stderr=""),  # get ns
                mock.Mock(returncode=0, stdout="", stderr=""),  # add role
                mock.Mock(returncode=1, stdout="", stderr="apply broke\n"),
            ]
            with self.assertRaises(UserError) as ctx:
                namespace.setup("oc", "mg")
        self.assertIn("apply broke", str(ctx.exception))

    def test_cleanup_can_keep_the_namespace(self):
        with mock.patch("must_gather_sos.namespace.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            namespace.cleanup("oc", "mg", delete_namespace=False)
        self.assertEqual(run.call_count, 1)
        self.assertIn("remove-cluster-role-from-user", run.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
