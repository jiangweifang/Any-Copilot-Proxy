from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from copilot_proxy.m365_copilot.benchmark import build_probe, execute, retrieved


class CharacterEncoding:
    def encode(self, text, *, disallowed_special=()):
        return list(text.encode("ascii"))

    def decode(self, tokens):
        return bytes(tokens).decode("ascii")


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def test_probes_include_start_middle_and_end_checkpoints_within_reported_budget(self):
        payload, expected, count = build_probe("m365/Chat", 2000, CharacterEncoding(), 123)
        text = payload["messages"][0]["content"]
        self.assertEqual(count, len(text))
        self.assertLessEqual(count, 2000)
        self.assertEqual(len(expected), 5)
        positions = [text.index(key + "=" + value) for key, value in expected.items()]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[0], count // 10)
        self.assertTrue(count * 0.4 < positions[2] < count * 0.6)
        self.assertGreater(positions[4], count * 0.85)
        self.assertTrue(retrieved(json.dumps(expected), expected))
        partial = dict(expected)
        partial["CHECKPOINT_2"] = "wrong"
        self.assertFalse(retrieved(json.dumps(partial), expected))

    async def test_dry_run_never_constructs_an_http_client(self):
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(root=Path(directory), model="m365/Chat", run=False, encoding="test-reference")
            with patch("copilot_proxy.m365_copilot.benchmark.SIZES", (1000, 2000)), \
                 patch("copilot_proxy.m365_copilot.benchmark.aiohttp.ClientSession") as client, \
                 redirect_stdout(io.StringIO()) as output:
                await execute(args, CharacterEncoding())
            client.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertEqual(report["mode"], "dry-run")
            self.assertFalse(report["officialTokenCounts"])
            self.assertEqual(len(report["probes"]), 2)
