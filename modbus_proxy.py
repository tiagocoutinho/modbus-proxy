import asyncio
from urllib.parse import urlparse

import click
from aiostream import stream, pipe


__version__ = "0.1.1"


async def run(surl, mburl):

    async def process(request):
        async with lock:
            writer.write(request)
            return await reader.read(4096)

    async def read(reader):
        while True:
            yield await reader.read(4096)

    async def cb(r, w):
        await (
            stream.iterate(read(r))
            | pipe.takewhile(lambda x: x)
            | pipe.map(process)
            | pipe.map(w.write)
        )

    lock = asyncio.Lock()
    reader, writer = await asyncio.open_connection(mburl.hostname, mburl.port)
    serv = await asyncio.start_server(cb, surl.hostname, surl.port)
    async with serv:
        await serv.serve_forever()


@click.command()
@click.option("-b", "--bind", "server", type=urlparse, default="tcp://0:5020")
@click.option("--modbus", type=urlparse, required=True)
def main(server, modbus):
    """Console script for modbus-proxy."""
    asyncio.run(run(server, modbus))


if __name__ == "__main__":
    main()
