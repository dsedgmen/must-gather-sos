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
        # If the outer shell had expanded the body, ${pids[@]} and friends
        # would have been emptied and the core phase would not have run.
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

    def test_one_line_per_plugin(self):
        self.collect(only="core,etcd,sriov,aro")
        with open(self.log_path(), encoding="utf-8") as fh:
            rows = [line.split() for line in fh if line.strip()]
        names = {row[0] for row in rows}
        self.assertEqual(
            names,
            {"core:named", "core:groups", "core:all-namespaces", "etcd", "sriov", "aro"},
        )
        for row in rows:
            self.assertEqual(row[1], "0")
            self.assertTrue(row[2].isdigit())

    def test_failures_are_recorded_without_aborting_the_run(self):
        proc = self.collect(only="etcd,sriov,aro", env={"STUB_RC": "7"})
        self.assertEqual(proc.returncode, 0)
        with open(self.log_path(), encoding="utf-8") as fh:
            rows = {line.split()[0]: line.split()[1] for line in fh if line.strip()}
        self.assertEqual(rows, {"etcd": "7", "sriov": "7", "aro": "7"})

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
        json.dumps(results)  # must be serialisable


if __name__ == "__main__":
    unittest.main()
