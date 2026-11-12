"""Unit tests for MustGather CR emission."""

from __future__ import annotations

import base64
import re
import unittest
from unittest import mock

from must_gather_sos import UserError, cli, mustgather_cr, runner
from must_gather_sos.mustgather_cr import CHUNK_SIZE, MAX_CHUNKS

from tests.test_cli import run_cli


class TestEncodeChunks(unittest.TestCase):
    def test_round_trip_through_bootstrap_args(self):
        script = "#!/bin/bash\necho hello-from-must-gather-sos\n"
        command, args = mustgather_cr.encode_chunks(script)
        self.assertEqual(command[:2], ["/bin/bash", "-c"])
        self.assertEqual(command[3], "--")
        self.assertLessEqual(len(command[2]), CHUNK_SIZE)
        self.assertTrue(args)
        self.assertTrue(all(len(c) <= CHUNK_SIZE for c in args))
        payload = "".join(args)
        self.assertEqual(
            base64.b64decode(payload).decode("utf-8"), script
        )

    def test_chunks_cover_a_large_script(self):
        script = "x" * (CHUNK_SIZE * 40)
        _command, args = mustgather_cr.encode_chunks(script)
        self.assertGreater(len(args), 1)
        self.assertLessEqual(len(args), MAX_CHUNKS)
        self.assertEqual(base64.b64decode("".join(args)).decode("utf-8"), script)

    def test_refuses_when_too_many_chunks(self):
        decoded_needed = (MAX_CHUNKS * CHUNK_SIZE * 3) // 4 + 100
        with self.assertRaises(UserError) as ctx:
            mustgather_cr.encode_chunks("y" * decoded_needed)
        self.assertIn("too large", str(ctx.exception))


class TestParseImageStream(unittest.TestCase):
    def test_name_tag(self):
        self.assertEqual(
            mustgather_cr.parse_image_stream("must-gather:4.18"),
            {"name": "must-gather", "tag": "4.18"},
        )

    def test_namespace_name_tag(self):
        self.assertEqual(
            mustgather_cr.parse_image_stream("openshift/must-gather:latest"),
            {"name": "must-gather", "tag": "latest"},
        )

    def test_rejects_bare_name(self):
        with self.assertRaises(UserError):
            mustgather_cr.parse_image_stream("must-gather")


class TestBuildAndRender(unittest.TestCase):
    def test_minimal_cr_has_embedded_script(self):
        script = "#!/bin/bash\ntrue\n"
        doc = mustgather_cr.build(script, name="demo")
        self.assertEqual(doc["apiVersion"], mustgather_cr.API_VERSION)
        self.assertEqual(doc["kind"], mustgather_cr.KIND)
        self.assertEqual(doc["metadata"]["name"], "demo")
        self.assertEqual(
            doc["spec"]["serviceAccountName"],
            mustgather_cr.DEFAULT_SERVICE_ACCOUNT,
        )
        gs = doc["spec"]["gatherSpec"]
        self.assertEqual(gs["command"][0], "/bin/bash")
        self.assertIn("args", gs)
        yaml = mustgather_cr.render_yaml(doc)
        self.assertIn("kind: MustGather", yaml)
        self.assertIn("gatherSpec:", yaml)
        self.assertIn("command:", yaml)
        self.assertIn("args:", yaml)
        payload = "".join(gs["args"])
        self.assertEqual(
            base64.b64decode(payload).decode("utf-8"),
            script,
        )
        # Quoted or bare — every chunk must appear in the YAML.
        for chunk in gs["args"]:
            self.assertIn(chunk, yaml)

    def test_case_id_adds_upload_target(self):
        doc = mustgather_cr.build(
            "#!/bin/bash\ntrue\n",
            case_id="04123456",
            case_secret="creds",
            internal_user=True,
        )
        sftp = doc["spec"]["uploadTarget"]["sftp"]
        self.assertEqual(sftp["caseID"], "04123456")
        self.assertEqual(sftp["caseManagementAccountSecretRef"]["name"], "creds")
        self.assertTrue(sftp["internalUser"])
        yaml = mustgather_cr.render_yaml(doc)
        self.assertIn("caseID: '04123456'", yaml)

    def test_since_timeout_image_stream_and_obfuscate(self):
        doc = mustgather_cr.build(
            "#!/bin/bash\ntrue\n",
            case_id="1",
            since="2h",
            timeout="30m",
            image_stream="openshift/must-gather:4.19",
            obfuscate=True,
            namespace="openshift-must-gather-operator",
        )
        self.assertEqual(doc["spec"]["gatherSpec"]["since"], "2h")
        self.assertEqual(doc["spec"]["mustGatherTimeout"], "30m")
        self.assertEqual(
            doc["spec"]["imageStreamRef"],
            {"name": "must-gather", "tag": "4.19"},
        )
        self.assertTrue(doc["spec"]["obfuscate"]["enabled"])
        self.assertEqual(
            doc["metadata"]["namespace"],
            "openshift-must-gather-operator",
        )

    def test_obfuscate_requires_case_id(self):
        with self.assertRaises(UserError):
            mustgather_cr.build("#!/bin/bash\ntrue\n", obfuscate=True)

    def test_yaml_quotes_bootstrap_safely(self):
        doc = mustgather_cr.build("#!/bin/bash\ntrue\n")
        yaml = mustgather_cr.render_yaml(doc)
        self.assertIn(runner.POD_SCRIPT, yaml)
        self.assertIn("base64 -d", yaml)


class TestPrintMustgatherCli(unittest.TestCase):
    def test_prints_cr_and_needs_no_cluster(self):
        with mock.patch("must_gather_sos.runner.which_oc") as which, \
             mock.patch("must_gather_sos.runner.check_cluster") as check:
            code, out, err = run_cli(
                "-o", "core,etcd", "--since", "2h",
                "--case-id", "04123456", "--print-mustgather",
            )
            which.assert_not_called()
            check.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("kind: MustGather", out)
        self.assertIn("caseID: '04123456'", out)
        self.assertRegex(out, r"since: ['\"]?2h['\"]?")
        # Recover base64 chunks from YAML list items under gatherSpec.args.
        chunks = []
        for line in out.splitlines():
            m = re.match(r"^\s+- ([A-Za-z0-9+/=]{16,})$", line)
            if m:
                chunks.append(m.group(1))
        self.assertTrue(chunks, "expected base64 args in printed CR")
        decoded = base64.b64decode("".join(chunks)).decode("utf-8")
        self.assertIn("/usr/bin/gather_etcd", decoded)

    def test_mutually_exclusive_print_modes(self):
        code, _out, err = run_cli(
            "-o", "core", "--print-mustgather", "--print-command",
        )
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err)

    def test_rejects_image_and_limit_resources(self):
        with self.assertRaises(UserError) as ctx:
            cli.validate(cli.build_parser().parse_args([
                "--print-mustgather", "--image", "quay.io/example/mg:latest",
            ]))
        self.assertIn("--image", str(ctx.exception))

        with self.assertRaises(UserError) as ctx:
            cli.validate(cli.build_parser().parse_args([
                "--print-mustgather", "--limit-resources",
            ]))
        self.assertIn("--limit-resources", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
