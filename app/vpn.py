import asyncio
import grp
import ipaddress
import json
import logging
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_from_bytes

from locations import SLUG, region_list
from proxy import Tunnel, usable_address

CLIENT = "/opt/expressvpn/bin/expressvpnctl"
DAEMON = "/opt/expressvpn/bin/expressvpn-daemon"
CLI_ENV = {
    "PATH": os.defpath + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/sbin",
    "HOME": "/root", "LD_LIBRARY_PATH": "/opt/expressvpn/lib", "LC_ALL": "C.UTF-8",
}
SETTINGS = Path("/opt/expressvpn/etc")
DATA = Path("/opt/expressvpn/var")
STATES = {
    "disconnected", "connecting", "connected", "interrupted", "reconnecting",
    "disconnectingtoreconnect", "disconnecting",
}


class CLIError(Exception):
    pass


class LoginError(CLIError):
    pass


@dataclass
class ConnectionChange:
    action: str
    location: str | None
    deadline: float
    previous_tunnel: Tunnel | None
    switching: bool
    sent: bool = False
    saw_transition: bool = False


def login_failure(output):
    output = output.casefold()
    patterns = {
        "account_portal": ("password login is not available", "verification required", "requires verification"),
        "subscription": ("subscription expired", "subscription has expired", "no active subscription"),
        "network": ("timed out", "network error", "could not resolve", "couldn't resolve", "unable to reach"),
        "client": ("could not open file:", "missing parameter:", "invalid login file"),
        "rejected": ("invalid activation code", "invalid login token", "invalid credentials", "incorrect password",
                     "unable to log in", "login failed", "authentication failed"),
    }
    return next((code for code, phrases in patterns.items() if any(phrase in output for phrase in phrases)), None)


async def run(*args, timeout=5, env=None, group=None, login=False, input_data=None):
    child = await asyncio.create_subprocess_exec(
        *args, env=env, group=group,
        stdin=asyncio.subprocess.PIPE if input_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(input_data), timeout)
        if login:
            reason = login_failure((stdout + b"\n" + stderr).decode(errors="replace"))
            if reason or child.returncode:
                raise LoginError(reason or "unconfirmed")
        if child.returncode:
            raise CLIError(f"命令退出（{child.returncode}）")
        return stdout.decode(errors="replace").strip()
    except TimeoutError:
        if login:
            raise LoginError("network") from None
        raise CLIError("命令超时") from None
    finally:
        if child.returncode is None:
            with suppress(ProcessLookupError):
                child.kill()
            await child.wait()


class ExpressVPN:
    def __init__(self, proxy, dns_override, preferred_protocol="lightwaytcp"):
        self.proxy = proxy
        self.dns_override = dns_override
        self.preferred_protocol = preferred_protocol
        self.configured = False
        self.ready = False
        self.state = "starting"
        self.signed_in = None
        self.location = None
        self.vpn_ip = None
        self.locations = []
        self.action = None
        self.change = None
        self.unconfirmed_change = None
        self.operation_error = None
        self.client_notice = None
        self.dns_configured = False
        self.dns_due = 0
        self.ip_due = 0
        self.wakeup = asyncio.Event()
        self.lock = asyncio.Lock()

    async def cli(self, *args, timeout=5):
        try:
            # Linux permits the daemon to inspect its CLI peer in /proc when their GIDs match.
            group = grp.getgrnam("expressvpn").gr_gid
            return await run(CLIENT, "--timeout", str(timeout), *args, timeout=timeout + 1,
                             env=CLI_ENV, group=group, login=args[0] == "login")
        except LoginError:
            raise
        except CLIError as error:
            command = " ".join(args[:2]) if args[0] in {"get", "set"} else args[0]
            raise CLIError(f"{command} {error}") from None

    async def login(self, lines):
        reason = "unconfirmed"
        with tempfile.NamedTemporaryFile(dir="/dev/shm", prefix="evpn-login-", mode="w", encoding="utf-8") as credential:
            credential.write("\n".join(lines) + "\n")
            credential.flush()
            try:
                await self.cli("login", credential.name, timeout=55)
            except LoginError as error:
                reason = str(error)
        for _ in range(10):
            if self.account_state() is True:
                self.signed_in = True
                with suppress(CLIError):
                    await self.load_locations()
                return
            await asyncio.sleep(0.5)
        raise LoginError(reason)

    def begin_connection(self, action, location=None):
        self.unconfirmed_change = None
        now = asyncio.get_running_loop().time()
        previous = self.proxy.tunnel
        switching = action == "connect" and self.state == "connected"
        if switching and location != "smart" and self.location == location and previous is not None:
            self.operation_error = None
            return
        self.change = ConnectionChange(
            action, location, now + (75 if action == "connect" else 30), previous, switching,
        )
        self.action = action
        self.operation_error = None
        self.dns_configured = False
        self.dns_due = self.ip_due = 0
        self.vpn_ip = None
        self.proxy.set_tunnel(None)
        self.wakeup.set()

    def finish_connection(self, change, error=None):
        if self.change is change:
            self.change = None
            self.action = None
            self.operation_error = error
            self.unconfirmed_change = change if error else None

    async def request_connection(self, change):
        change.sent = True
        args = ("connect", change.location) if change.action == "connect" else ("disconnect",)
        try:
            await self.cli(*args, timeout=7)
        except (CLIError, OSError) as error:
            # The daemon can finish a request after the one-shot CLI has timed out.
            logging.info("等待官方客户端确认连接操作。%s", error)

    async def test_proxy(self):
        user = quote_from_bytes(self.proxy.username, safe="")
        password = quote_from_bytes(self.proxy.password, safe="")
        config = f'proxy = "socks5h://{user}:{password}@127.0.0.1:1080"\n'
        trace = await run("curl", "-q", "--config", "-", "--noproxy", "", "--fail", "--silent",
                          "--max-time", "9", "--max-filesize", "8192",
                          "https://www.cloudflare.com/cdn-cgi/trace",
                          timeout=10, input_data=config.encode())
        try:
            address = next(line[3:] for line in trace.splitlines() if line.startswith("ip="))
            return str(ipaddress.ip_address(address))
        except (StopIteration, ValueError):
            raise CLIError("代理出口检查返回无效地址") from None

    async def load_locations(self):
        entries = region_list(await self.cli("get", "regions", timeout=8))
        self.locations = entries

    def account_state(self):
        try:
            account = json.loads((SETTINGS / "account.json").read_text())
            return account["loggedIn"] is True
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def resolver_addresses(self):
        if self.dns_override:
            return self.dns_override
        config = Path("/etc/resolv.conf").read_text()
        hosts = re.findall(r"^\s*nameserver\s+([^\s#]+)", config, re.MULTILINE)
        if "127.0.0.1" in hosts or "::1" in hosts:
            # Use the provider's upstreams with sockets bound to the tunnel.
            with suppress(OSError):
                config = (DATA / "unbound.conf").read_text()
                hosts.extend(re.findall(r"^\s*forward-addr:\s+([^\s#@]+)(?:@53)?(?:\s|$)", config, re.MULTILINE))
        servers = []
        for host in hosts:
            with suppress(ValueError):
                address = ipaddress.ip_address(host)
                if usable_address(address) and address not in servers:
                    servers.append(address)
        return tuple(servers[:3])

    async def find_tunnel(self, dns_ready):
        links = json.loads(await run("ip", "-j", "-d", "address", "show", "up"))
        candidates = [link for link in links
                      if (link.get("linkinfo") or {}).get("info_kind") in {"tun", "wireguard"}
                      and any(info["scope"] == "global" for info in link.get("addr_info", []))]
        if not candidates:
            return None
        if len(candidates) == 1:
            link = candidates[0]
        else:
            routes = json.loads(await run("ip", "-j", "route", "get", "1.1.1.1"))
            device = routes[0].get("dev") if routes else None
            link = next((link for link in candidates if link["ifname"] == device), None)
            if not link:
                return None
        resolvers = self.resolver_addresses() if dns_ready or self.dns_override else ()
        return Tunnel(link["ifname"], link["ifindex"], resolvers)

    async def refresh(self):
        if not self.configured:
            await self.cli("background", "enable")
            await self.cli("set", "allowlan", "true")
            await self.cli("set", "splittunnel", "false")
            await self.cli("set", "networklock", "true")
        change = self.change
        if change and not change.sent:
            await self.request_connection(change)
            if self.change is not change:
                return
        state = (await self.cli("get", "connectionstate", timeout=6)).casefold()
        if state not in STATES:
            raise CLIError("无法识别 VPN 状态")
        if not self.configured and state == "disconnected" and self.preferred_protocol:
            if (await self.cli("get", "protocol")).casefold() != self.preferred_protocol:
                await self.cli("set", "protocol", self.preferred_protocol)
        self.configured = True
        changed = self.state != state
        self.state = state
        self.ready = True
        self.client_notice = None
        self.signed_in = self.account_state()
        if self.change is not change:
            return
        if state != "connected":
            self.location = self.vpn_ip = None
            self.dns_configured = False
            self.dns_due = self.ip_due = 0
            self.proxy.set_tunnel(None)
            if change:
                change.saw_transition = True
                if change.action == "disconnect" and state == "disconnected":
                    self.finish_connection(change)
            if self.unconfirmed_change:
                self.unconfirmed_change.saw_transition = True
                if self.unconfirmed_change.action == "disconnect" and state == "disconnected":
                    self.unconfirmed_change = None
                    self.operation_error = None
        else:
            await self.refresh_tunnel(change, changed)
        if self.signed_in is False:
            self.locations = []
        elif self.signed_in and not self.locations and not self.change:
            with suppress(CLIError):
                await self.load_locations()

    async def refresh_tunnel(self, change, changed):
        if change and change.action == "disconnect":
            return
        tunnel = await self.find_tunnel(self.dns_configured)
        if tunnel is None:
            self.proxy.set_tunnel(None)
            return
        late = self.unconfirmed_change
        previous_tunnel = self.proxy.tunnel
        if change or changed or not self.location or (
            late and (previous_tunnel is None or previous_tunnel.index != tunnel.index)
        ):
            # "get region" reports the selected location, which can change before
            # the old tunnel is stopped. Only "status" confirms the active location.
            region = None
            with suppress(CLIError):
                status = await self.cli("status", timeout=4)
                match = re.search(r"^Connected to ([a-z0-9-]+)\.?\s*$", status, re.MULTILINE | re.IGNORECASE)
                if match and SLUG.fullmatch(match[1].casefold()):
                    region = match[1].casefold()
            if change:
                if not region or (change.location != "smart" and region != change.location):
                    return
                previous = change.previous_tunnel
                same_tunnel = previous is None or (tunnel.device, tunnel.index) == (previous.device, previous.index)
                if change.location == "smart" and change.switching and not change.saw_transition and same_tunnel:
                    with suppress(CLIError):
                        smart = await self.cli("get", "smart", timeout=3)
                        if region == smart:
                            same_tunnel = False
                    if same_tunnel:
                        return
            if self.change is not change:
                return
            if region:
                self.location = region
                if late and late.action == "connect" and (region == late.location or (
                    late.location == "smart" and (late.saw_transition or (
                        late.previous_tunnel is not None and late.previous_tunnel.index != tunnel.index
                    ))
                )):
                    self.unconfirmed_change = None
                    self.operation_error = None
        if self.change is not change:
            return
        now = asyncio.get_running_loop().time()
        self.proxy.set_tunnel(tunnel)
        if change:
            self.finish_connection(change)
        if now >= self.dns_due and (not self.dns_configured or not tunnel.resolvers):
            self.dns_due = now + 2
            with suppress(CLIError):
                self.dns_configured = (await self.cli("get", "dnsconfigured", timeout=3)).casefold() == "true"
                if self.change is None and self.proxy.tunnel is tunnel and self.dns_configured:
                    self.proxy.set_tunnel(Tunnel(tunnel.device, tunnel.index, self.resolver_addresses()))
        elif now >= self.ip_due:
            self.ip_due = now + 20
            with suppress(CLIError, ValueError):
                address = str(ipaddress.ip_address(await self.cli("get", "vpnip", timeout=2)))
                if self.change is None and self.proxy.tunnel is tunnel:
                    self.vpn_ip = address

    async def watch(self):
        while True:
            self.wakeup.clear()
            if not self.lock.locked():
                async with self.lock:
                    try:
                        await self.refresh()
                    except (CLIError, OSError, ValueError) as error:
                        if self.client_notice is None:
                            logging.warning("VPN 状态暂时不可读。%s", error)
                        self.client_notice = "官方客户端暂时无法返回状态，正在重新读取。"
                        # An already verified tunnel can outlive a slow control-IPC
                        # reply. Bound sockets still cannot route via a LAN interface.
                        actual = None
                        with suppress(CLIError, OSError, ValueError):
                            actual = await self.find_tunnel(self.dns_configured)
                        if self.change or actual != self.proxy.tunnel:
                            self.proxy.set_tunnel(None)
                    change = self.change
                    if change and asyncio.get_running_loop().time() >= change.deadline:
                        message = ("等待官方客户端确认目标隧道超时，可以重试或选择其他节点。"
                                   if change.action == "connect" else "暂未确认 VPN 已断开，可以重试。")
                        self.finish_connection(change, message)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wakeup.wait(), 1 if self.change else 2)

    def snapshot(self):
        tunnel = self.proxy.tunnel
        change = self.change
        stage = None
        if change:
            stage = "disconnecting" if change.action == "disconnect" else (
                "switching" if change.switching and self.state in {"connected", "disconnectingtoreconnect"} else "connecting")
        return {
            "ready": self.ready, "state": self.state, "signed_in": self.signed_in,
            "location": self.location, "vpn_ip": self.vpn_ip, "action": self.action,
            "action_location": change.location if change else None, "action_stage": stage,
            "operation_error": self.operation_error, "client_notice": self.client_notice,
            "proxy_ready": tunnel is not None, "dns_ready": bool(tunnel and tunnel.resolvers),
            "regions": self.locations,
        }
