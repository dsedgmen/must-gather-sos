import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

from must_gather_sos import UserError, archive, cli, manifest, script


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
                        volume_percentage=None, reduce_logs="", jobs=[])
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

    def test_jobs_accepts_zero_and_positive(self):
        # 0 is the documented "unlimited", not a mistake to reject.
        self.validate(jobs=["0"])
        self.validate(jobs=["4"])

    def test_jobs_rejects_negative(self):
        with self.assertRaises(UserError):
            self.validate(jobs=["-1"])

    def test_jobs_rejects_nonsense(self):
        with self.assertRaises(UserError):
            self.validate(jobs=["lots"])

    def test_jobs_can_name_an_image(self):
        self.validate(jobs=["4", "quay.io/x/mg:1=2"])


class TestFullRun(unittest.TestCase):
    """Drive _run with oc stubbed out, checking the post-collection steps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "must-gather.local.1")
        self.inner = os.path.join(self.dest, "image-sha")
        os.makedirs(self.inner)
        # What the in-pod task library writes: name, state, rc, seconds,
        # detail, tab separated.
        with open(os.path.join(self.inner, manifest.PLUGIN_LOG_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("core:named\tok\t0\t12\t\n"
                     "etcd\tfailed\t1\t3\trc=1\n"
                     "malformed line here\n")
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
        self.assertEqual(results["core:named"],
                         {"name": "core:named", "state": "ok", "rc": 0,
                          "seconds": 12})
        self.assertEqual(results["etcd"]["rc"], 1)
        self.assertEqual(results["etcd"]["state"], "failed")
        self.assertEqual(results["etcd"]["detail"], "rc=1")
        self.assertEqual(len(results), 2, "malformed lines must be ignored")

    def test_manifest_tallies_the_results_by_state(self):
        self.run_collect()
        with open(os.path.join(self.inner, manifest.MANIFEST_NAME),
                  encoding="utf-8") as fh:
            tally = json.load(fh)["results"]["tally"]
        self.assertEqual(tally, {"ok": 1, "no-op": 0, "failed": 1, "missing": 0})

    def test_plugin_failures_are_surfaced(self):
        _, _, err, _ = self.run_collect()
        self.assertIn("2 collection task(s)", err)
        self.assertIn("1 collected, 0 found nothing, 1 failed", err)
        self.assertIn("often harmless", err)
        self.assertIn("failed: etcd rc=1", err)

    def test_a_script_the_image_lacks_is_not_reported_as_a_failure(self):
        with open(os.path.join(self.inner, manifest.PLUGIN_LOG_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("etcd\tok\t0\t3\t\n"
                     "olm_v1\tmissing\t127\t0\tnot present in this image\n")
        _, _, err, _ = self.run_collect()
        self.assertIn("1 not in the image", err)
        self.assertIn("not present in this image: olm_v1", err)
        self.assertNotIn("1 failed.", err)

    def test_a_pre_progress_plugin_log_is_still_readable(self):
        # Space separated 'name rc seconds', with no verdict column. Written
        # by a must-gather-sos from before the sos-style progress landed;
        # someone re-reading an old archive should not get an empty manifest.
        with open(os.path.join(self.inner, manifest.PLUGIN_LOG_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write("core:named 0 12\netcd 1 3\n")
        self.run_collect()
        with open(os.path.join(self.inner, manifest.MANIFEST_NAME),
                  encoding="utf-8") as fh:
            results = json.load(fh)["results"]
        self.assertEqual([r["name"] for r in results["plugins"]],
                         ["core:named", "etcd"])
        self.assertEqual(results["tally"], {"ok": 1, "no-op": 0,
                                            "failed": 1, "missing": 0})

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


class TestTargets(unittest.TestCase):
    """Pairing images with the catalogs that describe them."""

    OPENSHIFT = "quay.io/openshift/origin-must-gather:4.18"
    ACM = "registry.redhat.io/rhacm2/acm-must-gather-rhel9:v2.14"

    def targets(self, **overrides):
        defaults = dict(image=[], catalog=[], jobs=[], only_plugins="",
                        skip_plugins="", enable_plugins="", all_plugins=False,
                        plugin_options=[])
        defaults.update(overrides)
        with captured():
            return cli.build_targets(argparse.Namespace(**defaults), None)

    def test_no_image_uses_the_payload_catalog(self):
        targets = self.targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].catalog.name, "openshift")
        self.assertIsNone(targets[0].image_ref)

    def test_an_image_picks_its_catalog_by_reference(self):
        targets = self.targets(image=[self.ACM])
        self.assertEqual([t.catalog.name for t in targets], ["acm"])

    def test_two_images_give_two_targets(self):
        targets = self.targets(image=[self.OPENSHIFT, self.ACM])
        self.assertEqual([t.catalog.name for t in targets],
                         ["openshift", "acm"])

    def test_a_lone_catalog_still_pairs_with_a_lone_image(self):
        # The documented fork workflow: --catalog better-gather --image <own>.
        targets = self.targets(image=["quay.io/me/my-gather:1"],
                               catalog=["better-gather"])
        self.assertEqual(targets[0].catalog.name, "better-gather")

    def test_a_catalog_can_be_pinned_to_one_image(self):
        targets = self.targets(image=["quay.io/me/my-gather:1", self.ACM],
                               catalog=["quay.io/me/my-gather:1=better-gather"])
        self.assertEqual([t.catalog.name for t in targets],
                         ["better-gather", "acm"])

    def test_an_unrecognised_image_asks_for_a_catalog(self):
        with self.assertRaises(UserError) as ctx:
            self.targets(image=["quay.io/me/mystery:1"])
        self.assertIn("--catalog", str(ctx.exception))

    def test_two_catalogs_of_the_same_family_are_refused(self):
        # The pod picks its branch by probing the filesystem, so two
        # catalogs for the same image layout cannot be told apart.
        with self.assertRaises(UserError) as ctx:
            self.targets(
                image=[self.OPENSHIFT, "quay.io/me/my-gather:1"],
                catalog=["quay.io/me/my-gather:1=better-gather"],
            )
        self.assertIn("same kind of image", str(ctx.exception))

    def test_a_plugin_name_need_only_exist_in_one_catalog(self):
        # -n is global; monitoring is OpenShift's, dr_logs is ACM's.
        targets = self.targets(image=[self.OPENSHIFT, self.ACM],
                               skip_plugins="monitoring,dr_logs")
        selected = {t.catalog.name: t.selection.names for t in targets}
        self.assertNotIn("monitoring", selected["openshift"])
        self.assertNotIn("dr_logs", selected["acm"])
        self.assertIn("etcd", selected["openshift"])
        self.assertIn("hub_logs", selected["acm"])

    def test_a_name_in_no_catalog_is_still_an_error(self):
        with self.assertRaises(UserError) as ctx:
            self.targets(image=[self.OPENSHIFT, self.ACM],
                         skip_plugins="no_such_plugin")
        self.assertIn("no_such_plugin", str(ctx.exception))

    def test_an_image_with_nothing_selected_is_dropped(self):
        # -o etcd means nothing to ACM; collect from OpenShift only.
        targets = self.targets(image=[self.OPENSHIFT, self.ACM],
                               only_plugins="etcd")
        self.assertEqual([t.catalog.name for t in targets], ["openshift"])

    def test_jobs_defaults_and_per_image_overrides(self):
        targets = self.targets(image=[self.OPENSHIFT, self.ACM],
                               jobs=["4", f"{self.ACM}=2"])
        self.assertEqual({t.catalog.name: t.jobs for t in targets},
                         {"openshift": 4, "acm": 2})

    def test_the_same_image_twice_is_refused(self):
        with self.assertRaises(UserError) as ctx:
            cli.validate(argparse.Namespace(
                since=None, since_time=None, image=[self.ACM, self.ACM],
                volume_percentage=None, reduce_logs="", jobs=[]))
        self.assertIn("twice", str(ctx.exception))


class TestMultiImageRun(unittest.TestCase):
    """Two images in one oc run: two output directories, two manifests.

    oc names each directory after the resolved image digest, so which is
    which comes from the marker the pod wrote, not from the name.
    """

    OPENSHIFT = "quay.io/openshift/origin-must-gather:4.18"
    ACM = "registry.redhat.io/rhacm2/acm-must-gather-rhel9:v2.14"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "must-gather.local.1")
        self.dirs = {}
        for profile, digest, rows in (
            ("openshift", "quay-io-openshift-sha256-aaa",
             "core:named\tok\t0\t12\t\netcd\tfailed\t1\t3\trc=1\n"),
            ("acm", "registry-redhat-io-rhacm2-sha256-bbb",
             "hub_logs\tok\t0\t9\t\ngovernance_logs\tno-op\t0\t1\tnot detected\n"),
        ):
            path = os.path.join(self.dest, digest)
            os.makedirs(path)
            self.dirs[profile] = path
            with open(os.path.join(path, manifest.PLUGIN_LOG_NAME), "w") as fh:
                fh.write(rows)
            with open(os.path.join(path, script.PROFILE_MARKER), "w") as fh:
                fh.write(profile + "\n")

    def run_collect(self, *extra, exit_code=0):
        patches = (
            mock.patch("must_gather_sos.runner.which_oc", return_value="/usr/bin/oc"),
            mock.patch("must_gather_sos.runner.check_cluster", return_value="me"),
            mock.patch("must_gather_sos.runner.execute", return_value=exit_code),
            mock.patch("must_gather_sos.runner.probe", return_value=None),
            mock.patch("must_gather_sos.cli.confirm", return_value=True),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with captured() as (out, err):
                code = cli.main(["--image", self.OPENSHIFT, "--image", self.ACM,
                                 "--dest-dir", self.dest, "--batch", *extra])
        return code, out.getvalue(), err.getvalue()

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_each_image_gets_its_own_manifest_attributed_by_the_marker(self):
        self.run_collect()
        for profile, expected_image in (("openshift", self.OPENSHIFT),
                                        ("acm", self.ACM)):
            with self.subTest(profile=profile):
                doc = self.read(os.path.join(self.dirs[profile],
                                             manifest.MANIFEST_NAME))
                self.assertEqual(doc["catalog"]["profile"], profile)
                # A scalar: a per-image manifest claiming both would be wrong.
                self.assertEqual(doc["collection"]["image"], expected_image)
                self.assertEqual(doc["manifest_version"],
                                 manifest.MANIFEST_VERSION)

    def test_a_run_manifest_maps_images_to_directories(self):
        self.run_collect()
        doc = self.read(os.path.join(self.dest, manifest.MANIFEST_NAME))
        self.assertEqual(
            {i["catalog"]: i["output_dir"] for i in doc["images"]},
            {"openshift": os.path.basename(self.dirs["openshift"]),
             "acm": os.path.basename(self.dirs["acm"])},
        )
        # The tally is every image's results added up.
        self.assertEqual(doc["results"]["tally"],
                         {"ok": 2, "no-op": 1, "failed": 1, "missing": 0})

    def test_failures_are_reported_against_the_image_they_came_from(self):
        _, _, err = self.run_collect()
        self.assertIn("[openshift]", err)
        self.assertIn("[acm]", err)
        self.assertIn("failed: etcd rc=1", err)

    def test_a_single_image_run_writes_no_run_manifest(self):
        # The layout for one image is exactly what it has always been.
        shutil.rmtree(self.dirs["acm"])
        patches = (
            mock.patch("must_gather_sos.runner.which_oc", return_value="/usr/bin/oc"),
            mock.patch("must_gather_sos.runner.check_cluster", return_value="me"),
            mock.patch("must_gather_sos.runner.execute", return_value=0),
            mock.patch("must_gather_sos.runner.probe", return_value=None),
            mock.patch("must_gather_sos.cli.confirm", return_value=True),
        )
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with captured():
                cli.main(["--image", self.OPENSHIFT, "--dest-dir", self.dest,
                          "--batch"])
        self.assertFalse(
            os.path.exists(os.path.join(self.dest, manifest.MANIFEST_NAME)))
        self.assertTrue(os.path.exists(
            os.path.join(self.dirs["openshift"], manifest.MANIFEST_NAME)))
