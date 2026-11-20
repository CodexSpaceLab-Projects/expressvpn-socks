import asyncio
import errno
import hmac
import ipaddress
import socket
import struct
from contextlib import suppress
from dataclasses import dataclass

import dns.exception
import dns.flags
import dns.message


@dataclass(frozen=True)
class Tunnel:
    device: str
    index: int
    resolvers: tuple


def usable_address(address):
    address = getattr(address, "ipv4_mapped", None) or address
    return not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast)


def bound_socket(tunnel, address, kind):
    if socket.if_nametoindex(tunnel.device) != tunnel.index:
        raise OSError(errno.ENETDOWN, "VPN interface changed")
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    sock = socket.socket(family, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, tunnel.device.encode() + b"\0")
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


async def dial(tunnel, address, port):
    sock = bound_socket(tunnel, address, socket.SOCK_STREAM)
    try:
        await asyncio.get_running_loop().sock_connect(sock, (str(address), port))
        return await asyncio.open_connection(sock=sock)
    except BaseException:
        sock.close()
        raise


async def close_stream(writer):
    writer.close()
    with suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 1)


async def copy_stream(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    writer.write_eof()


async def relay(client_reader, client_writer, remote_reader, remote_writer):
    uploads = asyncio.create_task(copy_stream(client_reader, remote_writer))
    downloads = asyncio.create_task(copy_stream(remote_reader, client_writer))
    try:
        await asyncio.gather(uploads, downloads)
    finally:
        uploads.cancel()
        downloads.cancel()
        await asyncio.gather(uploads, downloads, return_exceptions=True)


async def query_dns(tunnel, server, query):
    packet = query.to_wire()
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(2):
        with bound_socket(tunnel, server, socket.SOCK_DGRAM) as sock:
            await loop.sock_connect(sock, (str(server), 53))
            await loop.sock_sendall(sock, packet)
            raw = await loop.sock_recv(sock, 65535)
        response = dns.message.from_wire(raw)
        if response.flags & dns.flags.TC:
            reader, writer = await dial(tunnel, server, 53)
            try:
                writer.write(struct.pack("!H", len(packet)) + packet)
                await writer.drain()
                size, = struct.unpack("!H", await reader.readexactly(2))
                response = dns.message.from_wire(await reader.readexactly(size))
            finally:
                await close_stream(writer)
        if not query.is_response(response):
            raise dns.exception.FormError("DNS response does not match query")
        answer = response.resolve_chaining().answer
        return [ipaddress.ip_address(record.address) for record in answer] if answer else []


async def resolve(tunnel, host):
    async def lookup(record_type):
        query = dns.message.make_query(host + ".", record_type)
        for server in tunnel.resolvers:
            try:
                return await query_dns(tunnel, server, query)
            except (OSError, EOFError, dns.exception.DNSException):
                continue
        return []

    ipv4, ipv6 = await asyncio.gather(lookup("A"), lookup("AAAA"))
    return [address for address in (*ipv4, *ipv6) if usable_address(address)]


class SocksError(Exception):
    def __init__(self, reply):
        self.reply = reply


class SocksProxy:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.tunnel = None
        self.clients = set()

    def set_tunnel(self, tunnel):
        previous = self.tunnel
        self.tunnel = tunnel
        old_device = (previous.device, previous.index) if previous else None
        new_device = (tunnel.device, tunnel.index) if tunnel else None
        if old_device != new_device:
            for client in self.clients:
                client.cancel()

    async def authenticate(self, reader, writer):
        version, count = await reader.readexactly(2)
        if version != 5:
            return False
        methods = await reader.readexactly(count)
        if 2 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return False
        writer.write(b"\x05\x02")
        await writer.drain()
        version, size = await reader.readexactly(2)
        username = await reader.readexactly(size)
        size, = await reader.readexactly(1)
        password = await reader.readexactly(size)
        valid = hmac.compare_digest(username, self.username) & hmac.compare_digest(password, self.password)
        valid = valid and version == 1
        writer.write(b"\x01\x00" if valid else b"\x01\x01")
        await writer.drain()
        return valid

    async def request(self, reader):
        version, command, reserved, address_type = await reader.readexactly(4)
        if version != 5 or reserved != 0:
            raise SocksError(1)
        if command != 1:
            raise SocksError(7)
        if address_type == 3:
            size, = await reader.readexactly(1)
            hostname = (await reader.readexactly(size)).decode("ascii").rstrip(".")
            if not hostname:
                raise SocksError(8)
            address = hostname
        elif address_type in {1, 4}:
            size = 4 if address_type == 1 else 16
            address = ipaddress.ip_address(await reader.readexactly(size))
        else:
            raise SocksError(8)
        port, = struct.unpack("!H", await reader.readexactly(2))
        if not port or self.tunnel is None:
            raise SocksError(2)
        return address, port, self.tunnel

    async def connect(self, host, port, tunnel):
        if isinstance(host, str):
            addresses = await resolve(tunnel, host)
        else:
            addresses = [host] if usable_address(host) else []
        reply = 4
        for address in addresses:
            try:
                async with asyncio.timeout(4):
                    return await dial(tunnel, address, port)
            except OSError as error:
                if error.errno == errno.ECONNREFUSED:
                    reply = 5
        raise SocksError(reply)

    async def handle(self, reader, writer):
        if len(self.clients) >= 512:
            await close_stream(writer)
            return
        task = asyncio.current_task()
        self.clients.add(task)
        upstream = None
        try:
            async with asyncio.timeout(15):
                if not await self.authenticate(reader, writer):
                    return
                try:
                    address, port, tunnel = await self.request(reader)
                    remote_reader, upstream = await self.connect(address, port, tunnel)
                except (UnicodeError, dns.exception.DNSException):
                    raise SocksError(8) from None
                bound_ip, bound_port, *_ = upstream.get_extra_info("sockname")
                bound_address = ipaddress.ip_address(bound_ip)
                header = b"\x05\x00\x00" + (b"\x01" if bound_address.version == 4 else b"\x04")
                writer.write(header + bound_address.packed + struct.pack("!H", bound_port))
                await writer.drain()
            await relay(reader, writer, remote_reader, upstream)
        except SocksError as error:
            with suppress(OSError, TimeoutError):
                writer.write(bytes([5, error.reply, 0, 1]) + bytes(6))
                await asyncio.wait_for(writer.drain(), 1)
        except (OSError, EOFError):
            pass
        finally:
            self.clients.discard(task)
            if upstream:
                upstream.close()
            await close_stream(writer)
