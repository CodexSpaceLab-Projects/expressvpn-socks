import asyncio
import ipaddress
import stat
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from main import control_app
from proxy import SocksProxy, Tunnel
from vpn import CLIError, ExpressVPN, LoginError, login_failure, run


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def connected_vpn(self):
        proxy = SocksProxy(b"proxy", b"test-password")
        vpn = ExpressVPN(proxy, (ipaddress.ip_address("192.0.2.53"),))
        vpn.configured = True
        vpn.ready = True
        vpn.signed_in = True
        vpn.state = "connected"
        vpn.location = "singapore-cbd"
        vpn.locations = [{"id": "singapore-cbd"}, {"id": "japan-tokyo"}]
        proxy.set_tunnel(Tunnel("tun0", 10, vpn.dns_override))
        return vpn

    def test_dns_refresh_keeps_clients_on_the_same_tunnel(self):
        proxy = SocksProxy(b"proxy", b"test-password")
        proxy.set_tunnel(Tunnel("tun0", 10, ()))
        client = Mock()
        proxy.clients.add(client)
        proxy.set_tunnel(Tunnel("tun0", 10, (ipaddress.ip_address("192.0.2.53"),)))
        client.cancel.assert_not_called()
        proxy.set_tunnel(Tunnel("tun0", 11, ()))
        client.cancel.assert_called_once()

    async def test_web_access_without_admin_password(self):
        vpn = ExpressVPN(SimpleNamespace(tunnel=None), ())
        vpn.ready = True
        app = control_app(vpn, "proxy", 1080)
        async with TestClient(TestServer(app)) as client:
            for path in ("/", "/app.js", "/app.css", "/api/status"):
                async with client.get(path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertNotIn("WWW-Authenticate", response.headers)
            async with client.post("/api/control/test-proxy", json={}) as response:
                self.assertEqual(response.status, 403)
            async with client.post("/api/control/test-proxy", json={}, headers={"X-VPN-Control": "1"}) as response:
                self.assertEqual(response.status, 409)

    def test_login_diagnostics_are_classified(self):
        examples = {
            "Password login is not available. Run login with no arguments": "account_portal",
            "Subscription expired": "subscription",
            "Timed out after 5 seconds": "network",
            "Could not open file: account-input": "client",
            "Unable to log in.": "rejected",
            "Logging in with user password": None,
        }
        for output, code in examples.items():
            self.assertEqual(login_failure(output), code)

    async def test_exit_zero_with_provider_error_is_not_success(self):
        with self.assertRaisesRegex(LoginError, "^rejected$"):
            await run(sys.executable, "-c", "import sys; print('Unable to log in.', file=sys.stderr)", login=True)

    async def test_login_confirms_session_and_deletes_input(self):
        vpn = ExpressVPN(None, ())
        input_paths = []

        async def cli(command, *args, **options):
            if command == "login":
                path = Path(args[0])
                self.assertEqual(path.parent, Path("/dev/shm"))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(path.read_text(), "test-activation\n")
                input_paths.append(path)
            if command == "get":
                return "singapore-cbd"

        with patch.object(vpn, "cli", side_effect=cli), patch.object(vpn, "account_state", return_value=True):
            await vpn.login(["test-activation"])
        self.assertTrue(vpn.signed_in)
        self.assertFalse(input_paths[0].exists())
        self.assertEqual(vpn.locations[0]["id"], "singapore-cbd")

    async def test_no_session_after_cli_exit_zero(self):
        vpn = ExpressVPN(None, ())
        with patch.object(vpn, "cli", new=AsyncMock(return_value="")), patch.object(vpn, "account_state", return_value=False):
            with patch.object(asyncio, "sleep", new=AsyncMock()):
                with self.assertRaisesRegex(LoginError, "^unconfirmed$"):
                    await vpn.login(["test-activation"])
        self.assertIsNone(vpn.signed_in)

    async def test_connection_post_accepts_without_waiting_and_allows_disconnect(self):
        vpn = self.connected_vpn()
        entered_cli = asyncio.Event()
        release_cli = asyncio.Event()
        daemon_state = "Connected"

        async def cli(*args, **options):
            nonlocal daemon_state
            if args == ("connect", "japan-tokyo"):
                entered_cli.set()
                await release_cli.wait()
            elif args == ("disconnect",):
                daemon_state = "Disconnected"
            elif args == ("get", "connectionstate"):
                return daemon_state
            else:
                self.fail(f"Unexpected CLI call: {args}")

        async def refresh():
            async with vpn.lock:
                await vpn.refresh()

        headers = {"X-VPN-Control": "1"}
        with patch.object(vpn, "cli", side_effect=cli), patch.object(vpn, "account_state", return_value=True):
            async with TestClient(TestServer(control_app(vpn, "proxy", 1080))) as client:
                async with vpn.lock:
                    async with asyncio.timeout(2):
                        async with client.post("/api/control/connect", json={"location": "japan-tokyo"},
                                               headers=headers) as response:
                            self.assertEqual(response.status, 202)
                async with client.get("/api/status") as response:
                    state = await response.json()
                    self.assertEqual(state["action"], "connect")
                    self.assertEqual(state["action_location"], "japan-tokyo")
                    self.assertTrue(state["action_stage"])
                    self.assertFalse(state["proxy_ready"])
                task = asyncio.create_task(refresh())
                try:
                    async with asyncio.timeout(2):
                        await entered_cli.wait()
                        async with client.post("/api/control/connect", json={"location": "singapore-cbd"},
                                               headers=headers) as response:
                            self.assertEqual(response.status, 409)
                        async with client.post("/api/control/disconnect", json={}, headers=headers) as response:
                            self.assertEqual(response.status, 202)
                finally:
                    release_cli.set()
                    await asyncio.wait_for(task, 2)
                self.assertEqual(vpn.snapshot()["action"], "disconnect")
                self.assertFalse(vpn.snapshot()["proxy_ready"])
                await refresh()
                state = vpn.snapshot()
                self.assertEqual(state["state"], "disconnected")
                self.assertIsNone(state["action"])
                self.assertIsNone(state["operation_error"])
                self.assertFalse(state["proxy_ready"])

    async def test_cli_timeout_waits_for_verified_target_without_opening_old_tunnel(self):
        vpn = self.connected_vpn()
        daemon_state = "Connected"
        active_region = "singapore-cbd"
        old_tunnel = vpn.proxy.tunnel
        new_tunnel = Tunnel("tun0", 11, vpn.dns_override)
        tunnel = old_tunnel
        connect_calls = []

        async def cli(*args, **options):
            if args == ("connect", "japan-tokyo"):
                connect_calls.append(args)
                raise CLIError("timeout")
            if args == ("get", "connectionstate"):
                return daemon_state
            if args == ("status",):
                return f"Connected to {active_region}"
            if args == ("get", "dnsconfigured"):
                return "true"
            if args == ("get", "vpnip"):
                return "192.0.2.10"
            self.fail(f"Unexpected CLI call: {args}")

        async def find_tunnel(dns_ready):
            return tunnel

        with patch.object(vpn, "cli", side_effect=cli), patch.object(vpn, "account_state", return_value=True):
            with patch.object(vpn, "find_tunnel", side_effect=find_tunnel):
                vpn.begin_connection("connect", "japan-tokyo")
                self.assertFalse(vpn.snapshot()["proxy_ready"])
                await vpn.refresh()
                state = vpn.snapshot()
                self.assertTrue(state["ready"])
                self.assertEqual(state["action"], "connect")
                self.assertIsNone(state["operation_error"])
                self.assertFalse(state["proxy_ready"])

                daemon_state = "Connecting"
                await vpn.refresh()
                self.assertFalse(vpn.snapshot()["proxy_ready"])

                daemon_state = "Connected"
                tunnel = new_tunnel
                await vpn.refresh()
                self.assertEqual(vpn.snapshot()["action"], "connect")
                self.assertFalse(vpn.snapshot()["proxy_ready"])

                active_region = "japan-tokyo"
                await vpn.refresh()
                state = vpn.snapshot()
                self.assertEqual(state["location"], "japan-tokyo")
                self.assertTrue(state["proxy_ready"])
                self.assertIsNone(state["action"])
                self.assertIsNone(state["operation_error"])
        self.assertEqual(connect_calls, [("connect", "japan-tokyo")])

    async def test_optional_metadata_failure_keeps_verified_tunnel(self):
        proxy = Mock()
        dns_override = (ipaddress.ip_address("192.0.2.53"),)
        vpn = ExpressVPN(proxy, dns_override)
        vpn.configured = True
        vpn.dns_configured = True
        vpn.locations = [{"id": "singapore-cbd"}]
        commands = {"connectionstate": "Connected", "dnsconfigured": "true"}

        async def cli(command, subject=None, **options):
            if command == "status":
                return "Connected to singapore-cbd"
            self.assertEqual(command, "get")
            if subject == "vpnip":
                raise CLIError("timeout")
            return commands[subject]

        tunnel = Tunnel("tun0", 10, dns_override)
        with patch.object(vpn, "cli", side_effect=cli), patch.object(vpn, "account_state", return_value=True):
            with patch.object(vpn, "find_tunnel", new=AsyncMock(return_value=tunnel)):
                await vpn.refresh()
        self.assertTrue(vpn.ready)
        self.assertEqual(vpn.state, "connected")
        proxy.set_tunnel.assert_called_once_with(tunnel)

    async def test_late_confirmation_clears_a_switch_timeout(self):
        vpn = self.connected_vpn()
        vpn.begin_connection("connect", "japan-tokyo")
        vpn.finish_connection(vpn.change, "等待确认超时")
        commands = {
            ("get", "connectionstate"): "Connected",
            ("status",): "Connected to japan-tokyo",
            ("get", "dnsconfigured"): "true",
        }
        new_tunnel = Tunnel("tun0", 11, vpn.dns_override)
        with patch.object(vpn, "cli", side_effect=lambda *args, **options: commands[args]), patch.object(
            vpn, "account_state", return_value=True
        ), patch.object(vpn, "find_tunnel", new=AsyncMock(return_value=new_tunnel)):
            await vpn.refresh()
        self.assertEqual(vpn.snapshot()["location"], "japan-tokyo")
        self.assertTrue(vpn.snapshot()["proxy_ready"])
        self.assertIsNone(vpn.snapshot()["operation_error"])


if __name__ == "__main__":
    unittest.main()
