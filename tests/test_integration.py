"""End-to-end test of the generated script, without a cluster.

This builds a fake must-gather image on disk -- upstream's real ``common.sh``,
a ``version`` script, stub ``gather_*`` scripts and a stub ``oc`` -- then runs
the generated program *through the same mangling oc applies*: joined argv
dropped into an unquoted ``<<-MUSTGATHER_EOF`` heredoc fed to bash. If the
base64 trick in runner.encode_payload ever stops being necessary or sufficient,
these tests notice.

The only thing faked about the script itself is its absolute paths, rewritten
to point into the sandbox. Ordering, arguments, environment, common.sh
sourcing and the --since plumbing are exactly what would run in a pod.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from must_gather_sos import runner, script
from must_gather_sos.catalog import Catalog, build_selection

UPSTREAM_COMMON_SH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "upstream", "common.sh"
)

# Reproduces openshift/oc buildPodCommand(): the gather command is joined with
# spaces and placed in an unquoted heredoc, so the outer shell expands it.
OC_POD_COMMAND = """\
set -e
{checker}
if [ "$HAVE_SESSION_TOOLS" = "true" ]; then
  setsid -w bash <<-MUSTGATHER_EOF
{command}
MUSTGATHER_EOF
else
  {command}
fi
"""

# A stub gather_* script: records its name, arguments and the environment the
# generated script handed it.
STUB = """\
#!/bin/bash
printf '%s\\t%s\\n' "$(basename "$0")" "$*" >>"$RECORD/invocations"
printf '%s\\t%s\\t%s\\t%s\\n' "$(basename "$0")" \\
\t"${MUST_GATHER_NODES_INSPECTED:-}" \\
\t"${PROFILING_NODE_SECONDS:-}" \\
\t"${OUT:-}" >>"$RECORD/environment"
exit ${STUB_RC:-0}
"""

# A stub oc that records every invocation.
OC_STUB = """\
#!/bin/bash
printf '%s\\n' "$*" >>"$RECORD/oc-calls"
# 'oc get <resource>' is used by the core phase to filter resource groups.
if [ "$1" = "get" ]; then
\tcase "$2" in
\t*nonexistent*) exit 1 ;;
\t*) exit 0 ;;
\tesac
fi
exit 0
"""

VERSION_STUB = """\
#!/usr/bin/env bash
function version() {
\techo "4.99.0-test"
}
"""


class Sandbox:
    """A fake must-gather image rooted in a temp directory."""

    def __init__(self, catalog):
        self.root = tempfile.mkdtemp(prefix="mgs-int-")
        self.bin = os.path.join(self.root, "bin")
        self.record = os.path.join(self.root, "record")
        self.collect = os.path.join(self.root, "must-gather")
        for path in (self.bin, self.record, self.collect):
            os.makedirs(path, exist_ok=True)

        shutil.copyfile(UPSTREAM_COMMON_SH, os.path.join(self.bin, "common.sh"))
        self._write("version", VERSION_STUB)
        self._write("oc", OC_STUB)
        for name in catalog.names:
            if name != "core":
                self._write(f"gather_{name}", STUB)

    def _write(self, name, content):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(path, 0o755)

    def localise(self, text):
        """Point the script's absolute paths into the sandbox.

        Careful with /must-gather: a plain string replace would also hit the
        'openshift/must-gather' banner and the '/must-gather-sos-plugins.log'
        tail of a path already rewritten.
        """
        text = text.replace("/usr/bin/", self.bin + "/")
        text = re.sub(r"(?<![\w-])/must-gather/", self.collect + "/", text)
        text = re.sub(r"(?<![\w-])/must-gather(?![-\w])", self.collect, text)
        return text.replace("cd / || exit 1", f"cd {self.root} || exit 1")

    def run(self, text, env=None):
        """Run the script the way oc runs it, and return the completed process."""
        payload = runner.encode_payload(text).replace(
            runner.POD_SCRIPT, os.path.join(self.root, "run.sh")
        )
        pod_command = OC_POD_COMMAND.format(
            checker='HAVE_SESSION_TOOLS=$(command -v setsid >/dev/null && '
                    'command -v ps >/dev/null && echo true || echo false)',
            command=payload,
        )
        full_env = dict(os.environ)
        full_env.update({"RECORD": self.record, "PATH": self.bin + os.pathsep
                         + os.environ["PATH"]})
        full_env.update(env or {})
        return subprocess.run(["bash", "-c", pod_command], env=full_env,
                              capture_output=True, text=True, check=False)

    def read(self, name):
        path = os.path.join(self.record, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [line.rstrip("\n") for line in fh if line.strip()]

    def invocations(self):
        return dict(line.split("\t", 1) for line in self.read("invocations"))

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class IntegrationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.path.exists(UPSTREAM_COMMON_SH):
            raise unittest.SkipTest(
                "vendored upstream common.sh missing; run 'make sync-catalog'"
            )
        cls.catalog = Catalog.load()

    def setUp(self):
        self.sandbox = Sandbox(self.catalog)
        self.addCleanup(self.sandbox.cleanup)

    def collect(self, env=None, reduce_logs=None, **kwargs):
        selection = build_selection(self.catalog, **kwargs)
        text = script.generate(self.catalog, selection, reduce_logs=reduce_logs)
        proc = self.sandbox.run(self.sandbox.localise(text), env=env)
        self.assertEqual(proc.returncode, 0,
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc


class TestPayloadSurvivesOc(IntegrationTestCase):
    def test_script_runs_through_ocs_heredoc(self):
        self.collect(only="etcd,sriov")
        self.assertEqual(set(self.sandbox.invocations()),
                         {"gather_etcd", "gather_sriov"})

    def test_metacharacters_in_options_survive_intact(self):
        # $(...) and backticks would be executed by oc's outer shell if the
        # payload were not base64 encoded.
        selector = '{job="etcd"} $(touch /tmp/pwned) `id` $HOME'
        self.collect(only="metrics", raw_options=[f"metrics.match={selector}"])
        self.assertEqual(self.sandbox.invocations()["gather_metrics"],
                         f"--match={selector}")

    def test_the_script_body_itself_is_not_expanded(self):
        self.collect(only="core")
        # If the outer shell had expanded the body, ${task_pids[@]} and
        # friends would have been emptied and the core phase would not have
        # run.
        self.assertTrue(self.sandbox.read("oc-calls"))


class TestSincePlumbing(IntegrationTestCase):
    """--since works only if common.sh is sourced and its exports are used."""

    def test_since_reaches_oc_adm_inspect(self):
        self.collect(only="core", env={"MUST_GATHER_SINCE": "2h"})
        calls = self.sandbox.read("oc-calls")
        inspects = [c for c in calls if c.startswith("adm inspect")]
        self.assertTrue(inspects)
        for call in inspects:
            self.assertIn("--since=2h", call)

    def test_since_time_reaches_oc_adm_inspect(self):
        self.collect(only="core",
                     env={"MUST_GATHER_SINCE_TIME": "2026-10-16T09:00:00Z"})
        for call in self.sandbox.read("oc-calls"):
            if call.startswith("adm inspect"):
                self.assertIn("--since-time=2026-10-16T09:00:00Z", call)

    def test_no_since_means_no_since_argument(self):
        self.collect(only="core")
        for call in self.sandbox.read("oc-calls"):
            self.assertNotIn("--since", call)

    def test_rotated_pod_logs_on_by_default(self):
        self.collect(only="core")
        inspects = [c for c in self.sandbox.read("oc-calls")
                    if c.startswith("adm inspect")]
        self.assertTrue(all("--rotated-pod-logs" in c for c in inspects))

    def test_reduce_logs_skip_rotated_removes_the_flag(self):
        self.collect(only="core", reduce_logs=["skip_rotated_logs"])
        inspects = [c for c in self.sandbox.read("oc-calls")
                    if c.startswith("adm inspect")]
        self.assertTrue(inspects)
        self.assertFalse(any("--rotated-pod-logs" in c for c in inspects))

    def test_compress_service_logs_is_exported(self):
        self.collect(only="core", reduce_logs=["compress_service_logs"])
        # common.sh turns the option into an exported flag the plugins read.
        self.assertIn("compress_service_logs", self.read_script())

    def test_invalid_reduce_logs_value_is_rejected_by_common_sh(self):
        # common.sh exits 1 on an unknown REDUCE_LOGS option. Proving that
        # failure reaches us confirms our value really is what it parses.
        selection = build_selection(self.catalog, only="core")
        text = script.generate(self.catalog, selection, reduce_logs=["nonsense"])
        proc = self.sandbox.run(self.sandbox.localise(text))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REDUCE_LOGS unknown value", proc.stdout + proc.stderr)

    def read_script(self):
        with open(os.path.join(self.sandbox.root, "run.sh"),
                  encoding="utf-8") as fh:
            return fh.read()


class TestCorePhase(IntegrationTestCase):
    def test_three_inspect_calls(self):
        self.collect(only="core")
        inspects = [c for c in self.sandbox.read("oc-calls")
                    if c.startswith("adm inspect")]
        self.assertEqual(len(inspects), 3)

    def test_all_namespaces_call_present(self):
        self.collect(only="core")
        calls = self.sandbox.read("oc-calls")
        self.assertTrue(any("--all-namespaces" in c for c in calls))

    def test_group_resources_are_filtered_by_oc_get(self):
        self.collect(only="core")
        gets = [c for c in self.sandbox.read("oc-calls") if c.startswith("get ")]
        self.assertEqual(len(gets), len(self.catalog.core_resources["group_resources"]))

    def test_no_core_means_no_inspect(self):
        self.collect(only="etcd")
        self.assertFalse(self.sandbox.read("oc-calls"))


class TestPluginBehaviour(IntegrationTestCase):
    def test_upstream_default_arguments_are_passed(self):
        self.collect(only="service_logs")
        self.assertEqual(self.sandbox.invocations()["gather_service_logs"], "master")

    def test_plugin_options_become_arguments(self):
        self.collect(only="audit_logs", raw_options=["audit_logs.force=on"])
        self.assertEqual(self.sandbox.invocations()["gather_audit_logs"], "--force")

    def test_env_options_reach_the_plugin(self):
        self.collect(only="profiling_node",
                     raw_options=["profiling_node.seconds=60"])
        row = self.sandbox.read("environment")[0].split("\t")
        self.assertEqual(row[2], "60")

    def test_nodes_inspected_flag_follows_core(self):
        self.collect(only="core,ppc")
        self.assertEqual(self.sandbox.read("environment")[0].split("\t")[1], "true")

        self.sandbox.cleanup()
        self.sandbox = Sandbox(self.catalog)
        self.collect(only="ppc")
        self.assertEqual(self.sandbox.read("environment")[0].split("\t")[1], "")

    def test_post_phase_runs_after_the_parallel_phase(self):
        self.collect(only="etcd,aro")
        order = [line.split("\t")[0] for line in self.sandbox.read("invocations")]
        self.assertEqual(order[-1], "gather_aro")

    def test_defaults_run_every_default_plugin(self):
        self.collect()
        expected = {f"gather_{n}" for n in self.catalog.defaults if n != "core"}
        self.assertEqual(set(self.sandbox.invocations()), expected)

    def test_skipped_plugins_do_not_run(self):
        self.collect(skip="monitoring,istio,sriov")
        ran = set(self.sandbox.invocations())
        self.assertNotIn("gather_monitoring", ran)
        self.assertNotIn("gather_istio", ran)
        self.assertNotIn("gather_sriov", ran)
        self.assertIn("gather_etcd", ran)


class TestPluginLog(IntegrationTestCase):
    def log_path(self):
        return os.path.join(self.sandbox.collect,
                            os.path.basename(script.PLUGIN_LOG))

    def rows(self):
        """The results file as {name: (state, rc, seconds)}."""
        with open(self.log_path(), encoding="utf-8") as fh:
            parsed = [line.rstrip("\n").split("\t") for line in fh if line.strip()]
        for row in parsed:
            self.assertEqual(len(row), 5, row)
        return {row[0]: (row[1], row[2], row[3]) for row in parsed}

    def test_one_row_per_plugin(self):
        self.collect(only="core,etcd,sriov,aro")
        rows = self.rows()
        self.assertEqual(
            set(rows),
            {"core:named", "core:groups", "core:all-namespaces", "etcd", "sriov", "aro"},
        )
        for state, rc, seconds in rows.values():
            self.assertEqual(rc, "0")
            self.assertTrue(seconds.isdigit())
            # The stubs collect nothing and say nothing, which is exactly
            # what a no-op is.
            self.assertEqual(state, "no-op")

    def test_a_plugin_that_collects_something_is_recorded_as_ok(self):
        self.sandbox._write("gather_etcd",
                            "#!/bin/bash\necho 'collected 42 members'\n")
        self.collect(only="etcd")
        self.assertEqual(self.rows()["etcd"][0], "ok")

    def test_failures_are_recorded_without_aborting_the_run(self):
        proc = self.collect(only="etcd,sriov,aro", env={"STUB_RC": "7"})
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            self.rows(),
            {name: ("failed", "7", "0") for name in ("etcd", "sriov", "aro")},
        )

    def test_a_script_the_image_lacks_is_missing_rather_than_failed(self):
        # rc=127. The catalog describing a plugin the image does not carry is
        # a release mismatch, not a broken collection.
        os.unlink(os.path.join(self.sandbox.bin, "gather_sriov"))
        self.collect(only="etcd,sriov")
        self.assertEqual(self.rows()["sriov"][0], "missing")
        self.assertEqual(self.rows()["sriov"][1], "127")

    def test_version_file_written(self):
        self.collect(only="etcd")
        with open(os.path.join(self.sandbox.collect, "version"),
                  encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("openshift/must-gather", content)
        self.assertIn("4.99.0-test", content)
        self.assertIn("must-gather-sos", content)


class TestManifestFromRealRun(IntegrationTestCase):
    def test_manifest_parses_the_log_the_pod_actually_wrote(self):
        from must_gather_sos import manifest

        self.collect(only="etcd,sriov", env={"STUB_RC": "3"})
        results = manifest.read_plugin_log(self.sandbox.collect)
        self.assertEqual([r["name"] for r in results], ["etcd", "sriov"])
        self.assertTrue(all(r["rc"] == 3 for r in results))
        self.assertTrue(all(r["state"] == "failed" for r in results))
        self.assertEqual(manifest.tally(results)["failed"], 2)
        json.dumps(results)  # must be serialisable


class TestProgressOutput(IntegrationTestCase):
    """What the run actually prints, end to end through oc's heredoc."""

    def test_the_plan_and_the_summary_bracket_the_run(self):
        proc = self.collect(only="core,etcd,sriov")
        self.assertIn("Collecting with 5 task(s).", proc.stdout)
        self.assertIn("5 task(s):", proc.stdout)
        self.assertLess(proc.stdout.index("Collecting with"),
                        proc.stdout.index("[1/5]"))

    def test_every_task_reports_its_position(self):
        proc = self.collect(only="etcd,sriov,aro")
        for position in ("[1/3]", "[2/3]", "[3/3]"):
            self.assertIn(position, proc.stdout)

    def test_a_plugins_own_output_goes_to_its_own_file(self):
        self.sandbox._write("gather_etcd", "#!/bin/bash\necho ETCD-CHATTER\n")
        proc = self.collect(only="etcd")
        self.assertNotIn("ETCD-CHATTER", proc.stdout)
        with open(os.path.join(self.sandbox.collect, "task-logs", "etcd.log"),
                  encoding="utf-8") as fh:
            self.assertIn("ETCD-CHATTER", fh.read())

    def test_a_clean_copy_of_the_progress_is_left_in_the_archive(self):
        self.collect(only="etcd,sriov")
        path = os.path.join(self.sandbox.collect, "gather-progress.log")
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.startswith("  [")]
        self.assertEqual(len(lines), 2)

    def test_nothing_is_written_outside_the_collection_directory(self):
        # TASK_BASE_PATH is pinned by the generated script; if it were left
        # to the library's default this would land in the real /must-gather.
        self.collect(only="etcd")
        for name in ("task-logs", "gather-progress.log"):
            self.assertTrue(
                os.path.exists(os.path.join(self.sandbox.collect, name)), name
            )

    def test_compression_runs_as_a_tracked_task(self):
        proc = self.collect(only="core", reduce_logs=["compress_logs"])
        self.assertIn("compress", proc.stdout)
        self.assertIn("[4/4]", proc.stdout)


if __name__ == "__main__":
    unittest.main()


# A stand-in for the ACM image's /usr/bin/gather_utils: the detection helpers
# the real one defines, and the log function every script calls. The checks
# answer yes so the profile's setup has something to find.
ACM_UTILS = """\
#!/bin/bash
log() { printf '%s\\n' "$*"; }
check_if_engine() { export MCE_NAME=mce OPERATOR_NAMESPACE=o DEPLOYMENT_NAMESPACE=d; return 0; }
check_if_hub() { return 0; }
check_if_obs_hub() { return 0; }
check_if_spoke() { return 0; }
dump_hostedcluster() {
\tprintf 'dump_hostedcluster\\t%s\\t%s\\n' "${HC_NAME:-}" "${HC_NAMESPACE:-}" \\
\t\t>>"$RECORD/invocations"
\t[ -n "${HC_NAME:-}" ] || { log "Hosted cluster name was not provided. Skip collecting."; return 0; }
\techo "dumped ${HC_NAME}"
}
"""

# An ACM sub-script. Records the IS_* variables it was handed, which is the
# whole point: they decide what it collects, and only the gather entrypoint
# normally sets them.
ACM_STUB = """\
#!/bin/bash
printf '%s\\t%s\\t%s\\t%s\\t%s\\n' "$(basename "$0")" \\
\t"${IS_HUB:-unset}" "${IS_ENGINE:-unset}" "${IS_OBS_HUB:-unset}" \\
\t"${PRODUCT:-unset}" >>"$RECORD/acm-env"
echo "collected by $(basename "$0")"
"""


class AcmSandbox(Sandbox):
    """A fake ACM image: gather_utils and the stolostron collection scripts."""

    def __init__(self, catalog):
        self.root = tempfile.mkdtemp(prefix="mgs-acm-")
        self.bin = os.path.join(self.root, "bin")
        self.record = os.path.join(self.root, "record")
        self.collect = os.path.join(self.root, "must-gather")
        for path in (self.bin, os.path.join(self.bin, "mce"), self.record,
                     self.collect):
            os.makedirs(path, exist_ok=True)

        self._write("gather_utils", ACM_UTILS)
        self._write("oc", OC_STUB)
        for plugin in catalog.plugins.values():
            if plugin.script and plugin.script.startswith("/usr/bin/"):
                self._write(plugin.script[len("/usr/bin/"):], ACM_STUB)
        # The image's own entrypoint, for the unrecognised-image fallback.
        self._write("gather", ACM_STUB)


class TestAcmProfile(unittest.TestCase):
    """The ACM image is set up differently, and getting it wrong is silent.

    Its sub-scripts read IS_HUB / IS_ENGINE / IS_OBS_HUB and default them to
    false; only the gather entrypoint sets them. A generated script that
    skipped that would collect nothing -- and an empty log reads as 'no-op',
    so it would do so without complaint.
    """

    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load("acm")

    def setUp(self):
        self.sandbox = AcmSandbox(self.catalog)
        self.addCleanup(self.sandbox.cleanup)

    def collect(self, **kwargs):
        selection = build_selection(self.catalog, **kwargs)
        text = script.generate(self.catalog, selection)
        proc = self.sandbox.run(self.sandbox.localise(text))
        self.assertEqual(proc.returncode, 0,
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def env_rows(self):
        return {line.split("\t")[0]: line.split("\t")[1:]
                for line in self.sandbox.read("acm-env")}

    def test_every_sub_script_is_told_it_is_a_hub(self):
        self.collect(only="hub_logs,governance_logs,observability_logs")
        rows = self.env_rows()
        self.assertEqual(set(rows), {"gather_hub_logs",
                                     "gather_governance_logs",
                                     "gather_observability_logs"})
        for name, (is_hub, is_engine, is_obs, product) in rows.items():
            with self.subTest(script=name):
                self.assertEqual(is_hub, "true", "IS_HUB never reached it")
                self.assertEqual(is_engine, "true")
                self.assertEqual(is_obs, "true")
                self.assertEqual(product, "acm")

    def test_a_collecting_plugin_is_ok_not_a_silent_noop(self):
        proc = self.collect(only="hub_logs")
        self.assertIn("1 task(s): 1 ok, 0 no-op, 0 failed", proc.stdout)

    def test_the_mce_script_is_found_under_its_subdirectory(self):
        # /usr/bin/mce/gather_mce_logs, not /usr/bin/gather_mce_logs: the
        # catalog carries the real path rather than assuming the convention.
        proc = self.collect(only="mce_logs")
        self.assertIn("1 ok", proc.stdout)
        self.assertIn("gather_mce_logs", self.env_rows())

    def test_a_hosted_cluster_name_reaches_the_shell_function(self):
        # dump_hostedcluster is a function, which env(1) cannot run; this is
        # what mgs_with_env exists for.
        self.collect(only="hostedcluster",
                     raw_options=["hostedcluster.name=hc1",
                                  "hostedcluster.namespace=ns1"])
        row = dict(line.split("\t", 1) for line in
                   self.sandbox.read("invocations"))
        self.assertEqual(row["dump_hostedcluster"], "hc1\tns1")

    def test_without_a_name_the_hosted_cluster_plugin_is_a_noop(self):
        proc = self.collect(only="hostedcluster")
        self.assertIn("0 ok, 1 no-op", proc.stdout)

    def test_no_openshift_plumbing_leaks_into_the_acm_script(self):
        # Matched on code, not on words: the inlined task library's own
        # comments mention task_common.sh and would trip a bare substring.
        text = script.generate(
            self.catalog, build_selection(self.catalog, only="hub_logs"),
            reduce_logs=["compress_logs"],
        )
        for absent in ("source /usr/bin/common.sh", "get_log_collection_args",
                       "${log_collection_args}", "${rotated_pod_logs_arg}",
                       "export REDUCE_LOGS", "compress_after_gather",
                       "task_run compress"):
            self.assertNotIn(absent, text,
                             f"{absent!r} has no meaning in the ACM image")


class UnknownSandbox(Sandbox):
    """An image no catalog describes: no common.sh, no gather_utils."""

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="mgs-unknown-")
        self.bin = os.path.join(self.root, "bin")
        self.record = os.path.join(self.root, "record")
        self.collect = os.path.join(self.root, "must-gather")
        for path in (self.bin, self.record, self.collect):
            os.makedirs(path, exist_ok=True)
        self._write("oc", OC_STUB)
        # Its own entrypoint, which is what should end up running.
        self._write("gather", "#!/bin/bash\necho THE-IMAGES-OWN-GATHER\n")


class TestMultiImageDispatch(unittest.TestCase):
    """One script, several images.

    oc sends the same command to every --image pod, so the script has to
    recognise which image it woke up in. These run the *same generated text*
    against different fake images and check each one does its own thing.
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.exists(UPSTREAM_COMMON_SH):
            raise unittest.SkipTest("vendored upstream common.sh missing")
        cls.openshift = Catalog.load("openshift")
        cls.acm = Catalog.load("acm")

    def script_for(self, openshift_only="etcd,sriov", acm_only="hub_logs"):
        targets = [
            script.Target(self.openshift,
                          build_selection(self.openshift, only=openshift_only)),
            script.Target(self.acm, build_selection(self.acm, only=acm_only)),
        ]
        return script.generate_targets(targets)

    def run_in(self, sandbox, text):
        proc = sandbox.run(sandbox.localise(text))
        self.assertEqual(proc.returncode, 0,
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return proc

    def test_the_openshift_pod_runs_only_the_openshift_plugins(self):
        sandbox = Sandbox(self.openshift)
        self.addCleanup(sandbox.cleanup)
        proc = self.run_in(sandbox, self.script_for())
        self.assertEqual(set(sandbox.invocations()),
                         {"gather_etcd", "gather_sriov"})
        self.assertNotIn("gather_hub_logs", proc.stdout)

    def test_the_acm_pod_runs_only_the_acm_plugins(self):
        sandbox = AcmSandbox(self.acm)
        self.addCleanup(sandbox.cleanup)
        proc = self.run_in(sandbox, self.script_for())
        self.assertIn("[1/1] hub_logs", proc.stdout)
        self.assertNotIn("gather_etcd", proc.stdout)

    def test_each_pod_records_which_branch_it_took(self):
        # oc names its output directories after the resolved image digest,
        # which we cannot predict, so the pod says who it was.
        for sandbox, expected in ((Sandbox(self.openshift), "openshift"),
                                  (AcmSandbox(self.acm), "acm")):
            with self.subTest(profile=expected):
                self.addCleanup(sandbox.cleanup)
                self.run_in(sandbox, self.script_for())
                with open(os.path.join(sandbox.collect,
                                       script.PROFILE_MARKER)) as fh:
                    self.assertEqual(fh.read().strip(), expected)

    def test_an_unrecognised_image_runs_its_own_entrypoint(self):
        # Better than guessing: it is exactly what plain oc adm must-gather
        # would have done, so an extra --image is never worse than useless.
        sandbox = UnknownSandbox()
        self.addCleanup(sandbox.cleanup)
        proc = self.run_in(sandbox, self.script_for())
        self.assertIn("THE-IMAGES-OWN-GATHER", proc.stdout)
        self.assertIn("running its own entrypoint", proc.stdout)

    def test_a_single_image_script_has_no_dispatch_at_all(self):
        text = script.generate(self.openshift,
                               build_selection(self.openshift, only="etcd"))
        self.assertNotIn("mgs_detect", text)
        self.assertNotIn("exec /usr/bin/gather", text)

    def test_per_image_job_caps_land_in_their_own_branch(self):
        targets = [
            script.Target(self.openshift,
                          build_selection(self.openshift, only="etcd"),
                          None, 8),
            script.Target(self.acm,
                          build_selection(self.acm, only="hub_logs"), None, 2),
        ]
        text = script.generate_targets(targets)
        openshift_branch = text[text.index("mgs_run_openshift()"):
                                text.index("mgs_run_acm()")]
        acm_branch = text[text.index("mgs_run_acm()"):]
        self.assertIn("TASK_JOBS=8", openshift_branch)
        self.assertNotIn("TASK_JOBS=2", openshift_branch)
        self.assertIn("TASK_JOBS=2", acm_branch)
