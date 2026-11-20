import asyncio
import ipaddress
import json
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

from aiohttp import web

from proxy import SocksProxy, usable_address
from vpn import CLI_ENV, DAEMON, CLIError, ExpressVPN, LoginError

STATIC = Path(__file__).parent / "static"
ACTIONS = {"login", "logout", "connect", "disconnect", "refresh", "test-proxy"}
LOGIN_MESSAGES = {
    "account_portal": "该账户需要网页验证。请在 ExpressVPN 账户后台完成验证，然后复制 Linux 的激活码或登录令牌。",
    "subscription": "ExpressVPN 提示订阅已过期。请在账户后台检查订阅状态。",
    "network": "官方客户端登录请求超时或遇到网络问题。请稍后重试。",
    "client": "官方客户端无法读取登录输入。请重新提交。",
    "rejected": "ExpressVPN 拒绝了这次登录。请核对输入，或从账户后台重新获取 Linux 激活码或登录令牌。",
    "unconfirmed": "ExpressVPN 未确认登录。请在账户后台完成验证，再使用 Linux 激活码或登录令牌。",
}


def proxy_credentials():
    username = os.getenv("SOCKS_USER", "proxy").encode()
    if file := os.getenv("SOCKS_PASSWORD_FILE"):
        password = Path(file).read_bytes().rstrip(b"\r\n")
    else:
        password = os.getenv("SOCKS_PASSWORD", "").encode()
    if not password or any(byte in password for byte in (b"\x00", b"\r", b"\n")):
        raise ValueError("请通过 SOCKS_PASSWORD_FILE 或 SOCKS_PASSWORD 提供密码")
    if not 1 <= len(username) <= 255 or len(password) > 255:
        raise ValueError("SOCKS5 认证配置无效")
    return username, password


def credential_lines(payload):
    def field(name, limit, trim=True):
        value = payload.get(name)
        if not isinstance(value, str) or any(char in value for char in "\x00\r\n"):
            raise ValueError("登录字段格式有误")
        if trim:
            value = value.strip()
        if not 1 <= len(value) <= limit:
            raise ValueError("登录字段为空或过长")
        return value

    if payload.get("method") == "code":
        return [field("code", 2048)]
    if payload.get("method") == "email":
        email = field("email", 320)
        if "@" not in email:
            raise ValueError("请输入 ExpressVPN 账户邮箱")
        return [email, field("password", 1024, trim=False)]
    raise ValueError("请选择登录方式")


def control_app(vpn, socks_user, socks_port):
    @web.middleware
    async def require_control_header(request, handler):
        if request.method == "POST" and request.headers.get("X-VPN-Control") != "1":
            return web.json_response({"error": "请求来源无效"}, status=403)
        return await handler(request)

    async def headers(_request, response):
        response.headers.update({
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })

    async def health(_request):
        return web.Response(status=200 if vpn.ready else 503, text="ready" if vpn.ready else "waiting")

    async def status(_request):
        return web.json_response({**vpn.snapshot(), "socks_port": socks_port, "socks_user": socks_user})

    async def control(request):
        action = request.match_info["action"]
        if action not in ACTIONS:
            raise web.HTTPNotFound()
        try:
            if request.content_type != "application/json":
                raise ValueError("请求需使用 JSON")
            payload = await request.json(loads=json.loads)
            if not isinstance(payload, dict):
                raise ValueError("请求格式有误")
            if action == "login":
                values = credential_lines(payload)
            if action == "connect":
                location = payload.get("location")
                if location != "smart" and not any(region["id"] == location for region in vpn.locations):
                    raise ValueError("请从当前节点列表选择位置")
        except ValueError as error:
            message = "请求格式有误" if isinstance(error, json.JSONDecodeError) else str(error)
            return web.json_response({"error": message}, status=400)

        if vpn.action is not None and not (action == "disconnect" and vpn.action == "connect"):
            return web.json_response({"error": "正在处理另一项操作"}, status=409)
        if not vpn.ready:
            return web.json_response({"error": "ExpressVPN 服务暂未就绪"}, status=503)
        if action == "login" and vpn.signed_in:
            return web.json_response({"error": "请先退出当前 ExpressVPN 账户"}, status=409)
        if action == "connect" and not vpn.signed_in:
            return web.json_response({"error": "请先登录 ExpressVPN"}, status=409)
        if action == "test-proxy" and vpn.proxy.tunnel is None:
            return web.json_response({"error": "请先连接 VPN，再检测代理出口"}, status=409)
        if action in {"connect", "disconnect"}:
            vpn.begin_connection(action, location if action == "connect" else None)
            return web.json_response({"ok": True}, status=202)
        vpn.action = action
        try:
            async with vpn.lock:
                if action not in {"refresh", "test-proxy"}:
                    vpn.proxy.set_tunnel(None)
                if action == "login":
                    await vpn.login(values)
                elif action == "refresh":
                    await vpn.load_locations()
                elif action == "test-proxy":
                    return web.json_response({"ok": True, "ip": await vpn.test_proxy()})
                else:
                    await vpn.cli(action, timeout=30)
        except LoginError as error:
            code = str(error)
            return web.json_response({"error": LOGIN_MESSAGES.get(code, LOGIN_MESSAGES["unconfirmed"]), "code": code},
                                     status=502 if code in {"network", "client"} else 422)
        except (CLIError, OSError):
            messages = {
                "login": "网关暂时无法调用官方客户端。请等待服务就绪后重试。",
                "logout": "官方客户端退出登录失败或超时。",
                "refresh": "官方客户端暂时无法返回节点列表。",
                "test-proxy": "本次 HTTPS 出口检测未通过。可以重试检测或切换节点。",
            }
            return web.json_response({"error": messages[action]}, status=502)
        finally:
            vpn.action = None
            vpn.wakeup.set()
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[require_control_header], client_max_size=8192)
    app.on_response_prepare.append(headers)
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/control/{action}", control)

    async def static(request):
        name = request.path[1:] or "index.html"
        return web.FileResponse(STATIC / name)

    for path in ("/", "/app.js", "/app.css"):
        app.router.add_get(path, static)
    return app


def check_network():
    with open("/dev/net/tun", "rb+", buffering=0):
        pass
    capabilities = next(line.split()[1] for line in Path("/proc/self/status").read_text().splitlines()
                        if line.startswith("CapEff:"))
    if not int(capabilities, 16) & (1 << 12):
        raise ValueError("容器需要 NET_ADMIN 权限")


async def main():
    try:
        check_network()
        socks_user, socks_pass = proxy_credentials()
        socks_port = int(os.getenv("SOCKS_PUBLISHED_PORT", "1080"))
        if not 1 <= socks_port <= 65535:
            raise ValueError("SOCKS_PUBLISHED_PORT 超出端口范围")
        resolvers = tuple(ipaddress.ip_address(value) for value in os.getenv("SOCKS_DNS", "").replace(",", " ").split())
        if len(resolvers) > 3 or not all(usable_address(address) for address in resolvers):
            raise ValueError("SOCKS_DNS 必须为最多三个可通过隧道访问的 DNS 地址")
    except (ValueError, OSError) as error:
        logging.error("启动失败。%s", error)
        return 1

    proxy = SocksProxy(socks_user, socks_pass)
    vpn = ExpressVPN(proxy, resolvers, os.getenv("EXPRESSVPN_PROTOCOL", "lightwaytcp"))
    app = control_app(vpn, socks_user.decode(), socks_port)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3)
    daemon = await asyncio.create_subprocess_exec(
        DAEMON, env=CLI_ENV, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    tasks = {asyncio.create_task(vpn.watch()), asyncio.create_task(daemon.wait()), asyncio.create_task(stop.wait())}
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", 8080).start()
        server = await asyncio.start_server(proxy.handle, "0.0.0.0", 1080)
        async with server:
            logging.info("管理界面监听 8080，SOCKS5 监听 1080")
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            logging.error("ExpressVPN 服务意外停止")
            return 1
        return 0
    finally:
        proxy.set_tunnel(None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        if daemon.returncode is None:
            with suppress(ProcessLookupError):
                daemon.terminate()
            try:
                await asyncio.wait_for(daemon.wait(), 5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    daemon.kill()
                await daemon.wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(asyncio.run(main()))
