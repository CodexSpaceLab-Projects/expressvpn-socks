import asyncio
import sys

from proxy import close_stream, relay


async def forward(host, port, reader, writer):
    upstream = None
    try:
        async with asyncio.timeout(5):
            remote, upstream = await asyncio.open_connection(host, port)
        await relay(reader, writer, remote, upstream)
    except (OSError, EOFError):
        pass
    finally:
        if upstream:
            upstream.close()
        await close_stream(writer)


async def main(host):
    def web_client(reader, writer):
        return forward(host, 8080, reader, writer)

    def proxy_client(reader, writer):
        return forward(host, 1080, reader, writer)

    web_server = await asyncio.start_server(web_client, "0.0.0.0", 8080)
    async with web_server:
        proxy_server = await asyncio.start_server(proxy_client, "0.0.0.0", 1080)
        async with proxy_server:
            await asyncio.gather(web_server.serve_forever(), proxy_server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
