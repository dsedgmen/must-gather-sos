import argparse
import contextlib
import datetime
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from must_gather_sos import archive, cli, manifest


@contextlib.contextmanager
def captured():
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def run_cli(*argv):
    with captured() as (out, err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TestListPlugins(unittest.TestCase):
    def test_text_listing(self):
        code, out, _ = run_cli("--list-plugins")
        self.assertEqual(code, 0)
        self.assertIn("enabled by default:", out)
        self.assertIn("not enabled by default", out)
        self.assertIn("core", out)
        self.assertIn("audit_logs", out)
        self.assertIn("plugin options (-k plugin.option=value):", out)

    def test_long_plugin_name_does_not_collide_with_its_description(self):
        _, out, _ = run_cli("--list-plugins")
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("machineconfigdaemon_termination_logs"):
                self.assertEqual(stripped, "machineconfigdaemon_termination_logs")
                break
        else:
            self.fail("plugin not listed")

    def test_json_listing(self):
        code, out, _ = run_cli("--list-plugins", "--format", "json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertIn("core", doc["plugins"])
        self.assertTrue(doc["plugins"]["core"]["default"])
        self.assertFalse(doc["plugins"]["audit_logs"]["default"])
        self.assertIn("force", doc["plugins"]["audit_logs"]["options"])


class TestDryRun(unittest.TestCase):
    def test_prints_script_and_command(self):
        code, out, _ = run_cli("-o", "core,etcd", "--since", "2h",
                               "--dest-dir", "/tmp/x", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("--- in-pod script ---", out)
        self.assertIn("--- oc command ---", out)
        self.assertIn("source /usr/bin/common.sh", out)
        self.assertIn("/usr/bin/gather_etcd", out)
        self.assertIn("--since=2h", out)
        self.assertIn("one argv element", out)

    def test_dry_run_needs_no_cluster(self):
        with mock.patch("must_gather_sos.runner.which_oc") as which, \
             mock.patch("must_gather_sos.runner.check_cluster") as check:
            run_cli("-o", "core", "--dry-run")
            which.assert_not_called()
            check.assert_not_called()

    def test_skipped_plugins_are_reported(self):
        _, out, _ = run_cli("-n", "monitoring,istio", "--dry-run")
        self.assertRegex(out, r"skipping \(\d+\):.*istio")
        self.assertNotIn("/usr/bin/gather_istio", out)


class TestErrors(unittest.TestCase):
    def test_user_errors_exit_1_without_a_traceback(self):
        for argv in (("-o", "nope", "--dry-run"),
                     ("-o", "etcd", "-n", "istio", "--dry-run"),
                     ("--since", "soon", "--dry-run"),
                     ("--reduce-logs", "bogus", "--dry-run")):
            with self.subTest(argv=argv):
                code, _, err = run_cli(*argv)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("must-gather-sos: error:"), err)
                self.assertNotIn("Traceback", err)

    def test_bad_flag_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            with captured():
                cli.main(["--no-such-flag"])
        self.assertEqual(ctx.exception.code, 2)


class TestValidate(unittest.TestCase):
    def validate(self, **overrides):
        defaults = dict(since=None, since_time=None, image=[],
                        volume_percentage=None, reduce_logs="")
        defaults.update(overrides)
        return cli.validate(argparse.Namespace(**defaults))

    def test_accepts_oc_duration_forms(self):
        for value in ("30s", "5m", "2h", "1.5h", "100ms"):
            self.validate(since=value)

    def test_accepts_rfc3339(self):
        for value in ("2026-10-16T09:00:00Z", "2026-10-16T09:00:00+10:00",
                      "2026-10-16T09:00:00.123Z"):
            self.validate(since_time=value)

    def test_reduce_logs_returns_a_list(self):
        self.assertEqual(self.validate(reduce_logs="compress_logs,skip_rotated_logs"),
                         ["compress_logs", "skip_rotated_logs"])

    def test_volume_percentage_bounds(self):
        self.validate(volume_percentage=70)
        for bad in (0, 101):
            with self.assertRaises(Exception):
                self.validate(volume_percentage=bad)


class TestFullRun(unittest.TestCase):
    """Drive _run with oc stubbed out, checking the post-collection steps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "must-gather.local.1")
        self.inner = os.path.join(self.dest, "image-sha")
        os.makedirs(self.inner)
        with open(os.path.join(self.inner, manifest.PLUGIN_LOG_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("core:named 0 12\netcd 1 3\nmalformed line here\n")
        with open(os.path.join(self.inner, "version"), "w", encoding="utf-8") as fh:
            fh.write("openshift/must-gather\n")

    def _patches(self, exit_code=0):
        return (
            mock.patch("must_gather_sos.runner.which_oc", return_value="/usr/bin/oc"),
            mock.patch("must_gather_sos.runner.check_cluster", return_value="me"),
            mock.patch("must_gather_sos.runner.execute", return_value=exit_code),
            mock.patch("must_gather_sos.runner.probe", return_value=None),
            mock.patch("must_gather_sos.cli.confirm", return_value=True),
        )

    def run_collect(self, *extra, exit_code=0):
        with contextlib.ExitStack() as stack:
            patches = [stack.enter_context(p) for p in self._patches(exit_code)]
            with captured() as (out, err):
                code = cli.main(["-o", "core,etcd", "--dest-dir", self.dest,
                                 "--batch", *extra])
            return code, out.getvalue(), err.getvalue(), patches[2]

    def test_writes_manifest_and_script_into_the_nested_output_dir(self):
        code, _, _, _ = self.run_collect()
        self.assertEqual(code, 0)
        manifest_path = os.path.join(self.inner, manifest.MANIFEST_NAME)
        self.assertTrue(os.path.exists(manifest_path))
        self.assertTrue(os.path.exists(
            os.path.join(self.inner, manifest.SCRIPT_NAME)))

        with open(manifest_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(doc["selection"]["plugins"], ["core", "etcd"])
        self.assertIn("istio", doc["selection"]["skipped"])
        self.assertEqual(doc["results"]["exit_code"], 0)
        self.assertEqual(doc["tool"]["name"], "must-gather-sos")

    def test_manifest_records_per_plugin_results(self):
        self.run_collect()
        with open(os.path.join(self.inner, manifest.MANIFEST_NAME),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        results = {item["name"]: item for item in doc["results"]["plugins"]}
        self.assertEqual(results["core:named"], {"name": "core:named", "rc": 0,
                                                 "seconds": 12})
        self.assertEqual(results["etcd"]["rc"], 1)
        self.assertEqual(len(results), 2, "malformed lines must be ignored")

    def test_plugin_failures_are_surfaced(self):
        _, _, err, _ = self.run_collect()
        self.assertIn("collection steps exited non-zero", err)
        self.assertIn("usually harmless", err)
        self.assertIn("etcd: rc=1", err)

    def test_case_id_recorded(self):
        self.run_collect("--case-id", "04123456")
        with open(os.path.join(self.inner, manifest.MANIFEST_NAME),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["case_id"], "04123456")

    def test_no_manifest_flag(self):
        self.run_collect("--no-manifest")
        self.assertFalse(os.path.exists(
            os.path.join(self.inner, manifest.MANIFEST_NAME)))

    def test_archive_created_and_named_with_case_id(self):
        code, out, _, _ = self.run_collect("--case-id", "04123456", "--archive")
        self.assertEqual(code, 0)
        self.assertIn("archive:", out)
        tarballs = [f for f in os.listdir(self.tmp.name) if f.endswith(".tar.xz")]
        self.assertEqual(len(tarballs), 1)
        self.assertTrue(tarballs[0].startswith("must-gather-04123456-"))
        with tarfile.open(os.path.join(self.tmp.name, tarballs[0])) as tar:
            names = tar.getnames()
        self.assertTrue(any(n.endswith("/image-sha/version") for n in names))

    def test_oc_exit_code_is_propagated(self):
        code, _, _, _ = self.run_collect(exit_code=3)
        self.assertEqual(code, 3)

    def test_payload_reaches_oc_as_one_argument(self):
        _, _, _, execute = self.run_collect()
        argv = execute.call_args[0][0]
        self.assertEqual(argv[-2], "--")
        self.assertRegex(argv[-1], r"^echo '[A-Za-z0-9+/=]+' \| base64 -d >")


class TestArchiveHelpers(unittest.TestCase):
    def test_name_without_case_id(self):
        when = datetime.datetime(2026, 10, 16, 15, 4, 5)
        self.assertEqual(archive.archive_name(None, when),
                         "must-gather-20261016T150405Z.tar.xz")

    def test_name_formats(self):
        when = datetime.datetime(2026, 10, 16, 15, 4, 5)
        self.assertTrue(archive.archive_name("1", when, "gz").endswith(".tar.gz"))
        self.assertTrue(archive.archive_name("1", when, "none").endswith(".tar"))

    def test_human_size(self):
        self.assertEqual(archive.human_size(512), "512 B")
        self.assertEqual(archive.human_size(2048), "2.0 KiB")
        self.assertEqual(archive.human_size(5 * 1024 ** 3), "5.0 GiB")

    def test_create_rejects_missing_directory(self):
        with self.assertRaises(Exception):
            archive.create("/nonexistent", None, datetime.datetime.now())


if __name__ == "__main__":
    unittest.main()


class TestPrintModes(unittest.TestCase):
    def test_print_command_emits_one_pasteable_line(self):
        import shlex
        code, out, err = run_cli("-o", "core,etcd", "--since", "2h",
                                 "--dest-dir", "/tmp/x", "--print-command")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(len(out.strip().splitlines()), 1)
        argv = shlex.split(out)
        self.assertEqual(argv[:3], ["oc", "adm", "must-gather"])
        self.assertIn("--since=2h", argv)
        self.assertEqual(argv.index("--"), len(argv) - 2)

    def test_print_script_emits_only_the_script(self):
        code, out, err = run_cli("-o", "etcd", "--print-script")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertTrue(out.startswith("#!/bin/bash"))
        self.assertIn("/usr/bin/gather_etcd", out)
        self.assertNotIn("oc adm must-gather", out)

    def test_print_modes_need_no_cluster(self):
        with mock.patch("must_gather_sos.runner.check_cluster") as check:
            run_cli("-o", "core", "--print-command")
            run_cli("-o", "core", "--print-script")
            check.assert_not_called()


class TestSinceWarning(unittest.TestCase):
    def test_warns_that_audit_logs_forwards_since_to_no_effect(self):
        _, out, _ = run_cli("-o", "audit_logs", "-k", "audit_logs.force=on",
                            "--since", "30m", "--dry-run")
        self.assertIn("audit_logs forwards --since", out)
        self.assertIn("whole files are collected regardless", out)

    def test_warns_for_plugins_with_no_time_window(self):
        _, out, _ = run_cli("-o", "etcd", "--since", "30m", "--dry-run")
        self.assertIn("--since has no effect on etcd", out)

    def test_no_warning_when_every_plugin_honours_since(self):
        _, out, _ = run_cli("-o", "core,sriov", "--since", "30m", "--dry-run")
        self.assertNotIn("no effect", out)
        self.assertNotIn("forwards --since", out)

    def test_no_warning_without_since(self):
        _, out, _ = run_cli("-o", "audit_logs", "-k", "audit_logs.force=on",
                            "--dry-run")
        self.assertNotIn("no effect", out)
        self.assertNotIn("forwards --since", out)

    def test_listing_states_since_support_per_plugin(self):
        _, out, _ = run_cli("--list-plugins")
        self.assertIn("--since: no time window", out)
        self.assertIn("--since: pod logs only", out)
        # wrapped across lines, so match the distinctive fragment
        self.assertIn("oc/kubelet ignore it for files", " ".join(out.split()))
        self.assertIn("--since: pod logs only", out)
        self.assertIn("about --since:", out)

    def test_json_listing_carries_since_support(self):
        _, out, _ = run_cli("--list-plugins", "--format", "json")
        doc = json.loads(out)
        self.assertEqual(doc["plugins"]["audit_logs"]["since"], "passed-through")
        self.assertEqual(doc["plugins"]["core"]["since"], "pod-logs")
        self.assertEqual(doc["plugins"]["service_logs"]["since"], "journal")
        self.assertEqual(doc["plugins"]["ppc"]["since"], "both")
