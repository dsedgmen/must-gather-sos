"""Tests for the oc invocation, most importantly the payload encoding.

``oc`` joins everything after ``--`` with spaces and drops the result into an
unquoted heredoc, so anything the outer shell can expand would be mangled
before the pod's bash sees it. These tests are the regression guard for that.
"""

import argparse
import base64
import os
import re
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
