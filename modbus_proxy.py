import asyncio
import logging
import argparse
from urllib.parse import urlparse

__version__ = "0.2.0"

log = logging.getLogger("modbus-proxy")


class ConnectionClosedError(ConnectionError):
    pass


class Connection:

    def __init__(self, reader, writer):
        self._reader = reader
        self._writer = writer

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.close()

    @property
    def opened(self):
        return self._writer is not None and not self._writer.is_closing() and not self._reader.at_eof()

    async def close(self):
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._reader = None
            self._writer = None


class ModBus(Connection):

    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._lock = asyncio.Lock()
        self._log = log.getChild(f"modbus({host}:{port})")
        super().__init__(None, None)

    async def open(self):
        await self.close()
        self._log.info("connecting to modbus at %s...", (self._host, self._port))
        self._reader, self._writer = \
            await asyncio.open_connection(self._host, self._port)
        self._log.info("connected!")

    async def write(self, request):
        self._writer.write(request)
        await self._writer.drain()

    async def read(self):
        # TODO: make sure packet is complete
        reply = await self._reader.read(8192)
        if not reply:
            raise ConnectionClosedError("Modbus disconnected")
        return reply

    async def write_read(self, request):
        async with self._lock:
            await self.write(request)
            return await self.read()


class Client(Connection):

    async def read(self):
        # TODO: make sure packet is complete
        request = await self._reader.read(8192)
        if not request:
            raise ConnectionClosedError("Client disconnected")
        return request

    async def write(self, reply):
        self._writer.write(reply)
        await self._writer.drain()


async def run(server_url, modbus_url, timeout):

    async def handle_client(reader, writer):
        peer = writer.get_extra_info("peername")
        clog = log.getChild(f'Client({peer[0]}:{peer[1]})')
        clog.info("new connection")
        async with Client(reader, writer) as client:
            try:
                while True:
                    if not modbus.opened:
                        await asyncio.wait_for(modbus.open(), timeout)
                    request = await client.read()
                    if not modbus.opened:
                        await asyncio.wait_for(modbus.open(), timeout)
                    clog.debug("processing client to modbus")
                    reply = await asyncio.wait_for(
                        modbus.write_read(request), timeout
                    )
                    clog.debug("processing reply from modbus to client")
                    await client.write(reply)
            except ConnectionClosedError as closed:
                clog.info("%r", closed)
            except Exception as error:
                clog.error("%r", error)

    async with ModBus(modbus_url.hostname, modbus_url.port) as modbus:
        server = await asyncio.start_server(
            handle_client, server_url.hostname, server_url.port
        )
        async with server:
            log.info("Ready!")
            await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="ModBus proxy")
    parser.add_argument(
        "-b", "--bind", type=urlparse, default="tcp://0:5020",
        help="listen address (ex: tcp://0:502)"
    )
    parser.add_argument("--modbus", type=urlparse,
        help="modbus device address (ex: tcp://plc.acme.org:502)"
    )
    parser.add_argument("--timeout", type=float, default=None,
        help="modbus connection and request timeout (s)"
    )
    parser.add_argument("--log-level", default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help="modbus connection and request timeout (s)"
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s:%(levelname)7s:%(name)s:%(message)s",
        level=args.log_level.upper()
    )
    log.info("Starting...")
    try:
        asyncio.run(run(args.bind, args.modbus, args.timeout))
    except KeyboardInterrupt:
        print("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()
