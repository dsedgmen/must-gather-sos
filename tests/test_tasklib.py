"""Tests for data/task_common.sh -- the sos-style progress and summary.

Ported alongside the library itself from better-gather's tests/task_common.bats,
so a change on either side can be compared case for case.

Each test runs a short bash program with the library sourced and its paths
pointed at a temp directory, then asserts on what the run printed.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from must_gather_sos.script import TASK_LIB


def seq_names(count):
    return " ".join(f"t{i}" for i in range(1, count + 1))


class TaskLibTestCase(unittest.TestCase):
    """Base class giving each test a sandbox and a way to run bash in it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mgs-tasklib-")
        self.base = os.path.join(self.tmp, "mg")
        os.makedirs(self.base)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tasks(self, body, env=None, timeout=60, path=None):
        """Source the library and run *body*. Returns the CompletedProcess."""
        program = (
            f"export TASK_BASE_PATH={self.base!r}\n"
            f"source {TASK_LIB!r}\n"
            f"{body}\n"
        )
        environ = dict(os.environ)
        environ.pop("TASK_HEARTBEAT", None)
        if path is not None:
            environ["PATH"] = path
        environ.update(env or {})
        return subprocess.run(
            ["bash", "-c", program], capture_output=True, text=True,
            check=False, timeout=timeout, env=environ,
        )

    def run_tasks(self, body, **kwargs):
        """As tasks(), but assert success and return stdout."""
        proc = self.tasks(body, **kwargs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    @property
    def results(self):
        return os.path.join(self.base, "must-gather-sos-plugins.log")

    @property
    def log_dir(self):
        return os.path.join(self.base, "task-logs")


class TestPlan(TaskLibTestCase):
    def test_announces_how_many_tasks_and_which(self):
        out = self.run_tasks("task_plan etcd monitoring olm")
        self.assertIn("Collecting with 3 task(s).", out)
        for name in ("etcd", "monitoring", "olm"):
            self.assertIn(name, out)

    def test_says_where_the_per_task_output_goes(self):
        self.assertIn("task-logs/", self.run_tasks("task_plan etcd"))

    def test_starts_from_an_empty_results_file(self):
        out = self.run_tasks("""
            task_plan a
            task_run a true
            task_wait
            task_plan b
            echo "LINES=$(wc -l <"$TASK_RESULTS")"
        """)
        self.assertIn("LINES=0", out)


class TestProgress(TaskLibTestCase):
    def test_each_task_reports_its_position_out_of_the_total(self):
        out = self.run_tasks("""
            task_plan one two three
            task_run one true; task_wait
            task_run two true; task_wait
            task_run three true; task_wait
        """)
        for position in ("[1/3]", "[2/3]", "[3/3]"):
            self.assertIn(position, out)

    def test_the_position_is_padded_to_the_width_of_the_total(self):
        # 1 of 12 prints as [ 1/12], not [1/12], so the column stays aligned.
        out = self.run_tasks(f"""
            task_plan {seq_names(12)}
            task_run t1 true
            task_wait
        """)
        self.assertIn("[ 1/12]", out)


class TestClassification(TaskLibTestCase):
    def test_a_task_that_collects_something_is_ok(self):
        out = self.run_tasks("""
            task_plan alpha
            task_run alpha bash -c 'echo collected 42 objects'
            task_wait
            task_summary
        """)
        self.assertIn("1 task(s): 1 ok, 0 no-op, 0 failed", out)

    def test_a_task_that_says_the_thing_is_absent_is_a_noop(self):
        out = self.run_tasks("""
            task_plan sriov
            task_run sriov bash -c 'echo "INFO: sriov-network-operator not detected. Skipping."'
            task_wait
            task_summary
        """)
        self.assertIn("0 ok, 1 no-op, 0 failed", out)
        self.assertIn("collected nothing", out)
        self.assertIn("sriov", out)

    def test_a_task_that_produces_no_output_at_all_is_a_noop(self):
        out = self.run_tasks("""
            task_plan quiet
            task_run quiet true
            task_wait
            task_summary
        """)
        self.assertIn("0 ok, 1 no-op, 0 failed", out)
        self.assertIn("produced no output", out)

    def test_a_non_zero_exit_is_a_failure_with_its_log_named(self):
        proc = self.tasks("""
            task_plan broken
            task_run broken bash -c 'echo something went wrong >&2; exit 3'
            task_wait
            task_summary || true
        """)
        self.assertIn("0 ok, 0 no-op, 1 failed", proc.stdout)
        self.assertIn("rc=3", proc.stdout)
        self.assertIn("task-logs/broken.log", proc.stdout)

    def test_a_failure_is_explained_as_often_harmless(self):
        # oc adm inspect exits 1 whenever an optional resource is absent,
        # which is the common case; the summary should not read as a disaster.
        proc = self.tasks("""
            task_plan x
            task_run x bash -c 'exit 1'
            task_wait
            task_summary || true
        """)
        self.assertIn("often harmless", proc.stdout)

    def test_summary_returns_non_zero_when_a_task_failed(self):
        out = self.run_tasks("""
            task_plan x
            task_run x false
            task_wait
            if task_summary >/dev/null; then echo RC=0; else echo RC=nonzero; fi
        """)
        self.assertIn("RC=nonzero", out)

    def test_summary_returns_zero_when_nothing_failed(self):
        out = self.run_tasks("""
            task_plan x
            task_run x bash -c 'echo data'
            task_wait
            if task_summary >/dev/null; then echo RC=0; else echo RC=nonzero; fi
        """)
        self.assertIn("RC=0", out)

    def test_summary_copes_with_no_tasks_at_all(self):
        self.assertIn("No tasks were run.",
                      self.run_tasks("task_plan\ntask_summary"))


class TestOutputCapture(TaskLibTestCase):
    def test_a_tasks_output_goes_to_its_own_file_not_the_shared_log(self):
        out = self.run_tasks("""
            task_plan noisy
            task_run noisy bash -c 'echo LINE-FROM-THE-TASK; echo ERR-LINE >&2'
            task_wait
            echo '--- log contents ---'
            cat "$TASK_LOG_DIR/noisy.log"
        """)
        self.assertIn("ERR-LINE", out)
        # Once, in the file -- not twice from also being streamed.
        self.assertEqual(out.count("LINE-FROM-THE-TASK"), 1)

    def test_a_task_name_containing_a_slash_is_still_a_usable_filename(self):
        out = self.run_tasks("""
            task_plan core:named
            task_run core:named bash -c 'echo x'
            task_wait
            ls "$TASK_LOG_DIR"
        """)
        self.assertIn("core:named.log", out)

    def test_progress_is_also_written_to_a_log_without_ocs_noise(self):
        self.run_tasks("""
            task_plan a b >/dev/null
            task_run a bash -c 'echo data'
            task_run b bash -c 'echo data'
            task_wait
        """)
        with open(os.path.join(self.base, "gather-progress.log")) as fh:
            lines = [ln for ln in fh if ln.startswith("  [")]
        self.assertEqual(len(lines), 2)


class TestResultsFile(TaskLibTestCase):
    def test_results_are_tab_separated_name_state_rc_seconds_detail(self):
        self.run_tasks("""
            task_plan a
            task_run a bash -c 'echo data'
            task_wait
        """)
        with open(self.results) as fh:
            rows = [line.rstrip("\n").split("\t") for line in fh]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 5)
        self.assertEqual(rows[0][:3], ["a", "ok", "0"])

    def test_the_results_file_is_where_the_manifest_expects_it(self):
        # manifest.read_plugin_log reads this name out of the copied archive.
        self.run_tasks("task_plan a\ntask_run a true\ntask_wait")
        self.assertTrue(os.path.exists(self.results))


class TestDrift(TaskLibTestCase):
    def test_a_plan_that_disagrees_with_the_tasks_run_is_reported(self):
        out = self.run_tasks("""
            task_plan a b c
            task_run a true
            task_wait
            task_summary
        """)
        self.assertIn("WARNING", out)
        self.assertIn("drifted apart", out)

    def test_no_warning_when_the_plan_matches(self):
        out = self.run_tasks("""
            task_plan a b
            task_run a true
            task_run b true
            task_wait
            task_summary
        """)
        self.assertNotIn("drifted apart", out)


class TestParallelism(TaskLibTestCase):
    def test_tasks_really_do_run_in_parallel(self):
        out = self.run_tasks("""
            task_plan s1 s2 s3 >/dev/null
            start=$(date +%s)
            task_run s1 sleep 2
            task_run s2 sleep 2
            task_run s3 sleep 2
            task_wait
            echo "elapsed=$(( $(date +%s) - start ))"
        """)
        elapsed = int(re.search(r"elapsed=(\d+)", out).group(1))
        # Serially this would be 6s. Allow slack, but 4 proves concurrency.
        self.assertLess(elapsed, 4)

    def test_every_task_is_accounted_for_when_many_finish_at_once(self):
        # The position counter is serialised with flock; without it two tasks
        # finishing together can claim the same slot and one is lost.
        out = self.run_tasks(f"""
            task_plan {seq_names(20)} >/dev/null
            for i in $(seq 1 20); do task_run "t$i" true; done
            task_wait
            echo "LINES=$(wc -l <"$TASK_RESULTS")"
        """)
        self.assertIn("LINES=20", out)

    def test_positions_are_unique_under_heavy_concurrency(self):
        # 40 tasks finishing together. Without serialisation several claim
        # the same slot, which is how the need for flock showed up.
        out = self.run_tasks(f"""
            task_plan {seq_names(40)} >/dev/null
            for i in $(seq 1 40); do task_run "t$i" true; done
            task_wait
        """)
        positions = re.findall(r"\[ *(\d+)/40\]", out)
        self.assertEqual(len(positions), 40)
        self.assertEqual(len(set(positions)), 40)


class TestJobCap(TaskLibTestCase):
    """TASK_JOBS bounds how many tasks run at once, sos --threads style."""

    def test_unlimited_by_default(self):
        # The cap is opt-in: without it the library behaves as it always has,
        # which is what every other test in this file assumes.
        out = self.run_tasks("""
            echo "JOBS=$TASK_JOBS"
            task_plan s1 s2 s3 >/dev/null
            start=$(date +%s)
            task_run s1 sleep 2
            task_run s2 sleep 2
            task_run s3 sleep 2
            task_wait
            echo "elapsed=$(( $(date +%s) - start ))"
        """)
        self.assertIn("JOBS=0", out)
        self.assertLess(int(re.search(r"elapsed=(\d+)", out).group(1)), 4)

    def test_a_cap_actually_serialises(self):
        out = self.run_tasks(f"""
            task_plan {seq_names(9)} >/dev/null
            start=$(date +%s)
            for i in $(seq 1 9); do task_run "t$i" sleep 1; done
            task_wait
            echo "elapsed=$(( $(date +%s) - start ))"
        """, env={"TASK_JOBS": "3"}, timeout=60)
        elapsed = int(re.search(r"elapsed=(\d+)", out).group(1))
        # Nine 1s tasks, three at a time: about 3s. Unlimited would be 1,
        # fully serial would be 9.
        self.assertGreaterEqual(elapsed, 2)
        self.assertLess(elapsed, 7)

    def test_a_queued_task_has_not_started(self):
        # The gate is before the fork, so a task waiting for a slot is absent
        # from TASK_STARTED. If it were after the fork the heartbeat would
        # report queued tasks as running and their elapsed times would
        # include the wait.
        out = self.run_tasks(f"""
            task_plan {seq_names(6)} >/dev/null
            ( sleep 1; wc -l <"$TASK_STARTED" >"$TASK_BASE_PATH/sample" ) &
            sampler=$!
            for i in $(seq 1 6); do task_run "t$i" sleep 1; done
            task_wait
            wait "$sampler"
            echo "started=$(cat "$TASK_BASE_PATH/sample")"
        """, env={"TASK_JOBS": "2"}, timeout=60)
        # Two slots, one second into six one-second tasks: some have started
        # and some are still queued. All six would mean no gate at all.
        self.assertLess(int(re.search(r"started=(\d+)", out).group(1)), 6)

    def test_every_task_still_runs_and_is_recorded_exactly_once(self):
        out = self.run_tasks(f"""
            task_plan {seq_names(20)} >/dev/null
            for i in $(seq 1 20); do task_run "t$i" bash -c 'echo data'; done
            task_wait
            echo "LINES=$(wc -l <"$TASK_RESULTS")"
        """, env={"TASK_JOBS": "4"}, timeout=120)
        self.assertIn("LINES=20", out)
        positions = re.findall(r"\[ *(\d+)/20\]", out)
        self.assertEqual(len(set(positions)), 20)

    def test_a_killed_task_frees_its_slot(self):
        # Liveness is asked of the kernel, not inferred from TASK_RESULTS. A
        # task the OOM killer takes records nothing, and a gate that counted
        # results would wait for it forever.
        out = self.run_tasks("""
            task_plan victim after >/dev/null
            task_run victim bash -c 'kill -9 $$'
            task_run after bash -c 'echo data'
            task_wait
            echo DONE
        """, env={"TASK_JOBS": "1"}, timeout=60)
        self.assertIn("DONE", out)
        self.assertIn("after", out)

    def test_the_plan_says_the_cap_is_in_force(self):
        out = self.run_tasks("task_plan a b c", env={"TASK_JOBS": "2"})
        self.assertIn("Collecting with 3 task(s), 2 at a time.", out)
        plain = self.run_tasks("task_plan a b c")
        self.assertIn("Collecting with 3 task(s).", plain)

    def test_the_heartbeat_fires_during_a_capped_launch_phase(self):
        # The reason the heartbeat had to move out of task_wait: with a cap,
        # task_run blocks and the launch phase is most of the run.
        out = self.run_tasks("""
            task_plan a b c >/dev/null
            task_run a bash -c 'sleep 2; echo data'
            task_run b bash -c 'sleep 2; echo data'
            task_run c bash -c 'sleep 2; echo data'
            task_wait
        """, env={"TASK_JOBS": "1", "TASK_HEARTBEAT": "1"}, timeout=60)
        self.assertIn("still running:", out)
        self.assertIn("queued", out)

    def test_the_heartbeat_still_does_not_outlive_the_run(self):
        out = self.run_tasks("""
            task_plan a b >/dev/null
            task_run a bash -c 'sleep 1; echo data'
            task_run b bash -c 'sleep 1; echo data'
            task_wait
            sleep 2
            echo "running jobs after wait: $(jobs -rp | wc -l)"
        """, env={"TASK_JOBS": "1", "TASK_HEARTBEAT": "1"}, timeout=60)
        self.assertIn("running jobs after wait: 0", out)


class TestMissingScript(TaskLibTestCase):
    """rc=127 means the image does not carry that script.

    For must-gather-sos that is a catalog/image mismatch rather than a
    collection failure, and reporting it as a failure sends people looking
    for the wrong problem.
    """

    ABSENT = "/usr/bin/definitely-not-a-real-script"

    def test_a_missing_collection_script_is_reported_as_missing(self):
        out = self.run_tasks(f"""
            task_plan absent
            task_run absent {self.ABSENT}
            task_wait
            task_summary
        """)
        self.assertIn("1 missing", out)
        self.assertIn("not present in this image", out)
        self.assertNotIn("1 failed, 1 missing, in", out)

    def test_missing_scripts_are_counted_apart_from_failures(self):
        proc = self.tasks(f"""
            task_plan a b
            task_run a {self.ABSENT}
            task_run b bash -c 'exit 2'
            task_wait
            task_summary || true
        """)
        self.assertIn("0 ok, 0 no-op, 1 failed, 1 missing", proc.stdout)

    def test_the_mismatch_hint_points_at_the_image(self):
        out = self.run_tasks(f"""
            task_plan a
            task_run a {self.ABSENT}
            task_wait
            task_summary
        """)
        self.assertIn("--image", out)


class TestRecordingWithoutFlock(TaskLibTestCase):
    """A result must land whether or not flock cooperates."""

    SCENARIO = """
        task_plan a b c >/dev/null
        task_run a bash -c 'echo one'
        task_run b bash -c 'echo two'
        task_run c bash -c 'echo three'
        task_wait
        echo "RECORDED=$(wc -l <"$TASK_RESULTS")"
    """

    def _path_without_flock(self):
        """A symlink farm of the real PATH with flock removed."""
        farm = os.path.join(self.tmp, "nf")
        os.makedirs(farm, exist_ok=True)
        for directory in ("/usr/bin", "/bin"):
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                link = os.path.join(farm, name)
                if name == "flock" or os.path.lexists(link):
                    continue
                os.symlink(os.path.join(directory, name), link)
        return farm

    def test_results_land_when_flock_is_missing_entirely(self):
        farm = self._path_without_flock()
        self.assertFalse(os.path.exists(os.path.join(farm, "flock")))
        self.assertIn("RECORDED=3",
                      self.run_tasks(self.SCENARIO, path=farm))

    def test_results_land_when_flock_exists_but_fails(self):
        # Found by stubbing flock: the code took the flock branch, every call
        # exited 127, and every result was silently lost -- no records, no
        # summary, and nothing to say anything had gone wrong.
        stub_dir = os.path.join(self.tmp, "bf")
        os.makedirs(stub_dir, exist_ok=True)
        stub = os.path.join(stub_dir, "flock")
        with open(stub, "w") as fh:
            fh.write("#!/bin/sh\nexit 127\n")
        os.chmod(stub, 0o755)
        path = stub_dir + os.pathsep + os.environ["PATH"]
        self.assertIn("RECORDED=3", self.run_tasks(self.SCENARIO, path=path))

    def test_a_result_is_recorded_exactly_once_when_flock_works(self):
        # The fallback checks whether the record landed rather than trusting
        # flock's exit status, so it must not double-write on the happy path.
        if not shutil.which("flock"):
            self.skipTest("flock not installed")
        out = self.run_tasks("""
            task_plan solo
            task_run solo bash -c 'echo data'
            task_wait
            echo "LINES=$(wc -l <"$TASK_RESULTS")"
        """)
        self.assertIn("LINES=1", out)


class TestHeartbeat(TaskLibTestCase):
    def test_a_slow_task_does_not_leave_the_run_silent(self):
        # oc's own disk-usage checker prints every five seconds regardless,
        # so without this a long task looks like a hang.
        out = self.run_tasks("""
            task_plan quick slow >/dev/null
            task_run quick true
            task_run slow bash -c 'sleep 3; echo data'
            task_wait
        """, env={"TASK_HEARTBEAT": "1"})
        self.assertIn("still running: slow", out)
        self.assertIn("/2 done", out)

    def test_the_heartbeat_names_only_tasks_that_have_not_finished(self):
        out = self.run_tasks("""
            task_plan a b >/dev/null
            task_run a true
            task_run b bash -c 'sleep 3; echo data'
            task_wait
        """, env={"TASK_HEARTBEAT": "1"})
        running = [ln for ln in out.splitlines() if "still running" in ln]
        self.assertTrue(running)
        for line in running:
            self.assertNotIn(" a ", line)

    def test_the_heartbeat_can_be_turned_off(self):
        out = self.run_tasks("""
            task_plan slow >/dev/null
            task_run slow bash -c 'sleep 2; echo data'
            task_wait
        """, env={"TASK_HEARTBEAT": "0"})
        self.assertNotIn("still running", out)

    def test_the_heartbeat_does_not_outlive_task_wait(self):
        # A leaked background loop would keep printing into the pod log
        # after collection had finished.
        out = self.run_tasks("""
            task_plan a >/dev/null
            task_run a bash -c 'sleep 2; echo data'
            task_wait
            sleep 2
            echo "running jobs after wait: $(jobs -rp | wc -l)"
        """, env={"TASK_HEARTBEAT": "1"})
        self.assertIn("running jobs after wait: 0", out)

    def test_task_wait_returns_promptly_leaving_no_sleep_holding_the_log(self):
        # Killing the heartbeat does not kill a `sleep` it is blocked in, and
        # that orphan keeps the container's stdout open after collection has
        # finished. With the default 30s interval it stalled anything reading
        # the log to EOF for half a minute.
        out = self.run_tasks("""
            start=$(date +%s)
            task_plan a >/dev/null
            task_run a true
            task_wait
            echo "waited=$(( $(date +%s) - start ))"
        """, timeout=40)
        self.assertLess(int(re.search(r"waited=(\d+)", out).group(1)), 5)


if __name__ == "__main__":
    unittest.main()
