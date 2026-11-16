import asyncio
import importlib.util
from pathlib import Path
import unittest
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("meter", Path(__file__).parents[1] / "host/meter.py")
meter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meter)

def result(primary=None, secondary=None):
    return {"rateLimits": {"primary": primary, "secondary": secondary}}

class ProtocolTests(unittest.TestCase):
    def test_reset_countdown_and_window_length(self):
        data = meter.snapshot(result({"usedPercent": 25, "resetsAt": 8200, "windowDurationMins": 300},
                                     {"usedPercent": 60, "resetsAt": 260200, "windowDurationMins": 10080}), now=1000)
        self.assertEqual(meter.frame(data, now=1000), b"Q1 25 7200 300 60 259200 10080\n")
        self.assertEqual(meter.frame(data, now=1060), b"Q1 25 7140 300 60 259140 10080\n")

    def test_missing_window_never_looks_like_full_allowance(self):
        self.assertEqual(meter.frame(meter.snapshot(result(), now=0), now=0), b"Q1 -1 0 0 -1 0 0\n")

    def test_expired_window_does_not_refill(self):
        data = meter.snapshot(result({"usedPercent": 100, "resetsAt": 10, "windowDurationMins": 300}), now=11)
        self.assertEqual(meter.frame(data, now=11), b"Q1 100 0 300 -1 0 0\n")

    def test_bucket_selection(self):
        values = {"rateLimitsByLimitId": {"special": {"limitId": "special", "primary": None}}}
        self.assertEqual(meter.snapshot(values, "special")["bucket"], "special")
        with self.assertRaises(ValueError):
            meter.snapshot(values, "missing")

    def test_missing_limits_is_error(self):
        with self.assertRaises(ValueError):
            meter.snapshot({})

    def test_unusable_reset_is_unavailable(self):
        for reset in (None, float("nan"), float("inf")):
            data = meter.snapshot(result({"usedPercent": 20, "resetsAt": reset, "windowDurationMins": 300}))
            self.assertIsNone(data["windows"][0])

    def test_usb_ignores_cli_echo_and_accepts_ack(self):
        class Port:
            in_waiting = 0
            def __init__(self):
                self.parts = [b'codex_meter poll\r\n', b'\r\nMETER ', b'READY\r\n>: ']
                self.written = b''
            def reset_input_buffer(self): pass
            def write(self, data): self.written += data
            def flush(self): pass
            def read(self, _): return self.parts.pop(0)
        port = Port()
        self.assertEqual(meter.usb_command(port, b'poll'), b'READY')
        self.assertEqual(port.written, b'\r\ncodex_meter poll\r\n')

class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def test_ble_reassembles_hello_and_splits_update(self):
        class FakeClient:
            is_connected = True
            writes = []
            async def start_notify(self, _, callback):
                callback(None, bytearray(b"CODEXM"))
                callback(None, bytearray(b"ETER 1\n"))
            async def write_gatt_char(self, _, data, response):
                self.writes.append(data)
                if b"\n" in data:
                    self.is_connected = False
        class FakeCodex:
            async def call(self, method):
                return result({"usedPercent": 25, "resetsAt": time.time() + 7200, "windowDurationMins": 300})
        import time
        client = FakeClient()
        await meter.serve(client, FakeCodex(), SimpleNamespace(interval=60, bucket=None))
        self.assertTrue(all(len(chunk) <= 20 for chunk in client.writes))
        self.assertTrue(b"".join(client.writes).startswith(b"Q1 25 "))
        self.assertTrue(b"".join(client.writes).endswith(b"300 -1 0 0\n"))

    async def test_skips_notifications_and_matches_response_id(self):
        class Input:
            def __init__(self): self.lines = []
            def write(self, data): self.lines.append(data)
        class Proc: pass
        proc = Proc()
        proc.stdin = Input()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(b'{"method":"account/updated","params":{}}\n')
        proc.stdout.feed_data(b'{"id":1,"result":{"rateLimits":{}}}\n')
        codex = meter.Codex("unused")
        codex.proc = proc
        self.assertEqual(await codex.call("account/rateLimits/read"), {"rateLimits": {}})
        self.assertIn(b'account/rateLimits/read', proc.stdin.lines[0])

    async def test_rpc_errors_do_not_expose_server_payload(self):
        class Input:
            def write(self, data): pass
        class Proc: pass
        proc = Proc()
        proc.stdin = Input()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(b'{"id":1,"error":{"code":401,"message":"private-data"}}\n')
        codex = meter.Codex("unused")
        codex.proc = proc
        with self.assertRaises(RuntimeError) as context:
            await codex.call("account/rateLimits/read")
        self.assertNotIn("private-data", str(context.exception))

if __name__ == "__main__":
    unittest.main()
