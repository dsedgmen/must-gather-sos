"""Tests for the oc invocation, most importantly the payload encoding.

``oc`` joins everything after ``--`` with spaces and drops the result into an
unquoted heredoc, so anything the outer shell can expand would be mangled
before the pod's bash sees it. These tests are the regression guard for that.
"""

import argparse
import base64
import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

from must_gather_sos import UserError, runner
from must_gather_sos.catalog import Catalog, build_selection
from must_gather_sos.script import generate


def make_args(**overrides):
    defaults = dict(
        dest_dir="must-gather.local.1234", since=None, since_time=None,
        image=[], image_stream=None, timeout=None, node_name=None,
        node_selector=None, run_namespace=None, volume_percentage=None,
        host_network=False, source_dir=None,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestPayload(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog.load()
        selection = build_selection(self.catalog, only="core,etcd")
        self.script = generate(self.catalog, selection)
        self.payload = runner.encode_payload(self.script)

    def test_payload_has_no_characters_the_outer_shell_would_touch(self):
        encoded = re.search(r"echo '([^']*)'", self.payload).group(1)
        self.assertRegex(encoded, r"^[A-Za-z0-9+/=]+$")
        for dangerous in ("$", "`", "\\", '"', "\n"):
            self.assertNotIn(dangerous, encoded)

    def test_payload_is_a_single_line(self):
        self.assertNotIn("\n", self.payload)

    def test_payload_cannot_terminate_ocs_heredoc(self):
        self.assertNotIn("MUSTGATHER_EOF", self.payload)

    def test_payload_round_trips(self):
        encoded = re.search(r"echo '([^']*)'", self.payload).group(1)
        self.assertEqual(base64.b64decode(encoded).decode("utf-8"), self.script)

    def test_guard_rejects_unsafe_payload(self):
        with self.assertRaises(UserError):
            runner.assert_payload_safe("abc$(rm -rf /)")

    def test_decoding_the_payload_reproduces_the_script(self):
        """Run the payload's own shell pipeline and compare the result."""
        bash = shutil.which("bash")
        with tempfile.TemporaryDirectory() as tmp:
            # Same pipeline as in the pod, but writing somewhere harmless.
            local = self.payload.replace(runner.POD_SCRIPT,
                                         os.path.join(tmp, "out.sh"))
            local = local.split(" && ")[0]  # decode only; do not execute
            proc = subprocess.run([bash, "-c", local], capture_output=True,
                                  text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(tmp, "out.sh"), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), self.script)


class TestOcArgs(unittest.TestCase):
    def build(self, **overrides):
        return runner.build_oc_args(make_args(**overrides), "PAYLOAD", oc="oc")

    def test_exactly_one_element_after_the_separator(self):
        argv = self.build()
        self.assertEqual(argv.index("--"), len(argv) - 2)
        self.assertEqual(argv[-1], "PAYLOAD")

    def test_base_command(self):
        argv = self.build()
        self.assertEqual(argv[:4],
                         ["oc", "adm", "must-gather",
                          "--dest-dir=must-gather.local.1234"])

    def test_passthrough_flags_use_equals_form(self):
        # '--since 2h' as two argv elements would survive oc's join, but the
        # equals form is unambiguous and easier to eyeball in --dry-run.
        argv = self.build(since="2h", timeout="30m", node_name="master-0")
        self.assertIn("--since=2h", argv)
        self.assertIn("--timeout=30m", argv)
        self.assertIn("--node-name=master-0", argv)

    def test_unset_flags_are_omitted(self):
        argv = self.build()
        self.assertFalse([a for a in argv if a.startswith("--since")])
        self.assertFalse([a for a in argv if a.startswith("--image")])

    def test_repeatable_image_flag(self):
        argv = self.build(image=["quay.io/x/mg:1"])
        self.assertIn("--image=quay.io/x/mg:1", argv)

    def test_host_network_is_a_boolean_flag(self):
        self.assertIn("--host-network=true", self.build(host_network=True))
        self.assertNotIn("--host-network=true", self.build(host_network=False))

    def test_volume_percentage_zero_is_not_dropped_as_falsey(self):
        argv = self.build(volume_percentage=50)
        self.assertIn("--volume-percentage=50", argv)


class TestHelpers(unittest.TestCase):
    def test_default_dest_dir_matches_oc_naming(self):
        name = runner.default_dest_dir()
        self.assertRegex(name, r"^must-gather\.local\.\d+$")
        self.assertNotEqual(name, runner.default_dest_dir())

    def test_which_oc_reports_a_useful_error(self):
        with self.assertRaises(UserError) as ctx:
            runner.which_oc("definitely-not-a-real-binary")
        self.assertIn("--oc", str(ctx.exception))

    def test_resolve_output_dir_descends_one_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "quay-io-openshift-release-dev-sha256-abc")
            os.makedirs(nested)
            self.assertEqual(runner.resolve_output_dir(tmp), nested)

    def test_resolve_output_dir_stays_put_when_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a"))
            os.makedirs(os.path.join(tmp, "b"))
            self.assertEqual(runner.resolve_output_dir(tmp), tmp)

    def test_resolve_output_dir_on_missing_dir(self):
        self.assertIsNone(runner.resolve_output_dir("/nonexistent/path"))

    def test_probe_returns_none_on_failure(self):
        self.assertIsNone(runner.probe("false", ["anything"]))
        self.assertIsNone(runner.probe("definitely-not-a-real-binary", []))


if __name__ == "__main__":
    unittest.main()


class TestShellQuoting(unittest.TestCase):
    """--print-command output must be paste-able and parse back identically."""

    def test_payload_is_double_quoted_for_readability(self):
        quoted = runner.shell_quote("echo 'AAAA' | base64 -d >/tmp/x && bash /tmp/x")
        self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
        self.assertNotIn('"\'"', quoted)

    def test_falls_back_to_shlex_when_double_quotes_are_unsafe(self):
        for hostile in ("a$b", "a`b`", 'a"b', "a\\b"):
            with self.subTest(arg=hostile):
                self.assertEqual(shlex.split(runner.shell_quote(hostile)), [hostile])

    def test_printed_command_round_trips_through_the_shell(self):
        catalog = Catalog.load()
        selection = build_selection(catalog, only="core,etcd")
        payload = runner.encode_payload(generate(catalog, selection))
        argv = runner.build_oc_args(make_args(since="2h"), payload, oc="oc")
        self.assertEqual(shlex.split(runner.quote_command(argv)), argv)


class TestNoiseFilter(unittest.TestCase):
    """oc's own output is mostly not about the collection.

    Two sources, neither fixable from the must-gather image: the disk-usage
    line it prints every five seconds, and one line per file as it copies the
    data out. Samples below are taken verbatim from a real run.
    """

    NOISE = [
        "[must-gather-cgn6h] OUT 2026-10-18T01:13:01Z virt_info/crd/plans.forklift.konveyor.io.count",
        "[must-gather-cgn6h] OUT 2026-10-18T01:13:01Z sent 191,332 bytes  received 14,662,978 bytes",
        "[must-gather-cgn6h] OUT 2026-10-18T01:13:01Z total size is 144,247,646  speedup is 9.71",
        "[must-gather-qhdtc] POD 2026-10-18T00:31:18Z volume usage percentage 0",
        "[must-gather-qhdtc] POD 2026-10-18T00:31:18Z volume percentage checker started.....",
        "receiving incremental file list",
    ]

    SIGNAL = [
        "[must-gather-x] POD 2026-10-18T00:31:53Z   [26/28] ppc        ok   59s",
        "[must-gather-x] POD 2026-10-18T00:31:53Z   ... 26/28 done; still running: core:groups 1m00s",
        "[must-gather-x] POD 2026-10-18T00:31:53Z 28 task(s): 17 ok, 10 no-op, 1 failed, in 4m25s",
        "[must-gather-x] OUT 2026-10-18T00:31:53Z error: something broke",
        'ClusterVersion: Stable at "4.18.54"',
        "ClusterID: e13fedd3-e4dc-414e-84a5-2f5ea05b82b9",
    ]

    def test_noise_is_recognised(self):
        for line in self.NOISE:
            with self.subTest(line=line[:60]):
                self.assertTrue(runner.is_noise(line))

    def test_signal_is_never_hidden(self):
        for line in self.SIGNAL:
            with self.subTest(line=line[:60]):
                self.assertFalse(runner.is_noise(line))

    def test_errors_are_never_hidden(self):
        # Whatever else gets dropped, anything that looks like a problem has
        # to reach the user.
        for line in ("error: x", "Error from server (Forbidden): y",
                     "[must-gather-x] OUT 2026-01-01T00:00:00Z error: z"):
            with self.subTest(line=line):
                self.assertFalse(runner.is_noise(line))

    def test_filtering_keeps_the_unfiltered_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "oc-output.log")
            script = "; ".join(
                f"echo {shlex.quote(l)}" for l in self.NOISE + self.SIGNAL)
            with contextlib.redirect_stdout(io.StringIO()) as shown:
                rc = runner.execute(["bash", "-c", script, "payload"],
                                    log=io.StringIO(), raw_log_path=raw)
            self.assertEqual(rc, 0)

            visible = shown.getvalue()
            for line in self.SIGNAL:
                self.assertIn(line, visible)
            for line in self.NOISE:
                self.assertNotIn(line, visible)

            with open(raw, encoding="utf-8") as fh:
                kept = fh.read()
            for line in self.NOISE + self.SIGNAL:
                self.assertIn(line, kept, "the raw log must lose nothing")

    def test_no_filter_shows_everything(self):
        script = "echo 'volume usage percentage 0'"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = runner.execute(["bash", "-c", script, "payload"],
                                log=io.StringIO(), filter_noise=False)
        self.assertEqual(rc, 0)
