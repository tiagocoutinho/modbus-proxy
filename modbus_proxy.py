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
        self.reader = reader
        self.writer = writer

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.close()

    @property
    def opened(self):
        return self.writer is not None and not self.writer.is_closing() and not self.reader.at_eof()

    async def close(self):
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
            self.reader = None
            self.writer = None

    async def write(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def read(self):
        # TODO: make sure packet is complete
        reply = await self.reader.read(8192)
        if not reply:
            raise ConnectionClosedError("disconnected")
        return reply


class ModBus(Connection):

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.log = log.getChild(f"ModBus({host}:{port})")
        super().__init__(None, None)

    async def open(self):
        await self.close()
        self.log.info("connecting to modbus at %s...", (self.host, self.port))
        self.reader, self.writer = \
            await asyncio.open_connection(self.host, self.port)
        self.log.info("connected!")


class Client(Connection):

    def __init__(self, reader, writer):
        super().__init__(reader, writer)
        peer = writer.get_extra_info("peername")
        self.log = log.getChild(f'Client({peer[0]}:{peer[1]})')


class Server:

    def __init__(self, host, port, modbus, timeout=None):
        self.host = host
        self.port = port
        self.modbus = modbus
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def ensure_modbus_connection(self):
        async with self.lock:
            if not self.modbus.opened:
                await asyncio.wait_for(self.modbus.open(), self.timeout)

    async def modbus_write_read(self, data):
        async with self.lock:
            return await asyncio.wait_for(
                self._modbus_write_read(data), self.timeout
            )

    async def _modbus_write_read(self, data):
        await self.modbus.write(data)
        return await self.modbus.read()

    async def handle_client(self, reader, writer):
        async with Client(reader, writer) as client:
            log = client.log
            log.info('new connection')
            try:
                while True:
                    await self.ensure_modbus_connection()
                    request = await client.read()
                    await self.ensure_modbus_connection()
                    log.debug("processing client to modbus")
                    reply = await self.modbus_write_read(request)
                    log.debug("processing reply from modbus to client")
                    await client.write(reply)
            except ConnectionClosedError as closed:
                log.info("%r", closed)
            except Exception as error:
                log.error("%r", error)

    async def serve_forever(self):
        server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )
        async with server:
            await server.serve_forever()


async def run(server_url, modbus_url, timeout):
    async with ModBus(modbus_url.hostname, modbus_url.port) as modbus:
        server = Server(server_url.hostname, server_url.port, modbus, timeout)
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
