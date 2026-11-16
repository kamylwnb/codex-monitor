import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "host"))
import desktop
import meter

class DesktopHelpers(unittest.TestCase):
    def test_only_official_https_login_targets(self):
        self.assertTrue(desktop.official_login_url("https://auth.openai.com/authorize?state=example"))
        self.assertTrue(desktop.official_login_url("https://chatgpt.com/login"))
        for url in ["http://auth.openai.com", "https://auth.openai.com.attacker.test/",
                    "https://evilopenai.com/", "file:///tmp/login", "javascript:alert(1)"]:
            self.assertFalse(desktop.official_login_url(url))

    def test_reset_duration(self):
        self.assertEqual(desktop.duration(3660), "1 godz. 01 min")
        self.assertEqual(desktop.duration(-10), "mniej niż minutę")

class LoginFlow(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_requested_login(self):
        client = meter.Codex("unused")
        class Process: pass
        client.proc = Process()
        client.proc.stdout = asyncio.StreamReader()
        client.proc.stdout.feed_data(b'{"method":"account/login/completed","params":{"loginId":"other","success":true}}\n')
        client.proc.stdout.feed_data(b'{"method":"account/login/completed","params":{"loginId":"target","success":true}}\n')
        await client.wait_for_login("target")

    async def test_rejected_login_does_not_report_success(self):
        client = meter.Codex("unused")
        class Process: pass
        client.proc = Process()
        client.proc.stdout = asyncio.StreamReader()
        client.proc.stdout.feed_data(b'{"method":"account/login/completed","params":{"loginId":"target","success":false}}\n')
        with self.assertRaises(RuntimeError):
            await client.wait_for_login("target")
