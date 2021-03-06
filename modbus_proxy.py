import asyncio
import pathlib
import argparse
import logging.config
from urllib.parse import urlparse

__version__ = "0.4.1"


DEFAULT_LOG_CONFIG = {
    "version": 1,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)8s %(name)s: %(message)s"
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "root": {
        "handlers": ['console'],
        "level": "INFO"
    }
}

log = None


class Connection:

    def __init__(self, name, reader, writer):
        self.name = name
        self.reader = reader
        self.writer = writer
        self.log = log.getChild(name)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.close()

    @property
    def opened(self):
        return self.writer is not None and not self.writer.is_closing() and not self.reader.at_eof()

    async def close(self):
        if self.writer is not None:
            self.log.info("closing connection...")
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as error:
                self.log.info("failed to close: %r", error)
            else:
                self.log.info("connection closed")
            finally:
                self.reader = None
                self.writer = None

    async def _write(self, data):
        self.log.debug("sending %r", data)
        self.writer.write(data)
        await self.writer.drain()

    async def write(self, data):
        try:
            await self._write(data)
        except Exception as error:
            self.log.error("writting error: %r", error)
            await self.close()
            return False
        return True

    async def _read(self):
        """Read ModBus TCP message"""
        # TODO: Handle Modbus RTU and ASCII
        header = await self.reader.readexactly(6)
        size = int.from_bytes(header[4:], "big")
        reply = header + await self.reader.readexactly(size)
        self.log.debug("received %r", reply)
        return reply

    async def read(self):
        try:
            return await self._read()
        except asyncio.IncompleteReadError as error:
            if error.partial:
                self.log.error("reading error: %r", error)
            else:
                self.log.info("client closed connection")
            await self.close()
        except Exception as error:
            self.log.error("reading error: %r", error)
            await self.close()


class Client(Connection):

    def __init__(self, reader, writer):
        peer = writer.get_extra_info("peername")
        super().__init__(f"Client({peer[0]}:{peer[1]})", reader, writer)
        self.log.info("new client connection")


class ModBus(Connection):

    def __init__(self, host, port, modbus_host, modbus_port, timeout=None):
        super().__init__(f"ModBus({modbus_host}:{modbus_port})", None, None)
        self.host = host
        self.port = port
        self.modbus_host = modbus_host
        self.modbus_port = modbus_port
        self.timeout = timeout
        self.lock = asyncio.Lock()

    async def open(self):
        self.log.info("connecting to modbus...")
        self.reader, self.writer = \
            await asyncio.open_connection(self.modbus_host, self.modbus_port)
        self.log.info("connected!")

    async def write_read(self, data, attempts=2):
        async with self.lock:
            for i in range(attempts):
                try:
                    if not self.opened:
                        await asyncio.wait_for(self.open(), self.timeout)
                    coro = self._write_read(data)
                    return await asyncio.wait_for(coro, self.timeout)
                except Exception as error:
                    self.log.error("write_read error: %r", error)
                    await self.close()

    async def _write_read(self, data):
        await self._write(data)
        return await self._read()

    async def handle_client(self, reader, writer):
        async with Client(reader, writer) as client:
            while True:
                request = await client.read()
                if not request:
                    return
                reply = await self.write_read(request)
                if not reply:
                    return
                result = await client.write(reply)
                if not result:
                    return

    async def serve_forever(self):
        server = await asyncio.start_server(
            self.handle_client, self.host, self.port
        )
        async with server:
            self.log.info("Ready to accept requests on %s:%d", self.host, self.port)
            await server.serve_forever()


async def run(server_url, modbus_url, timeout):
    async with ModBus(server_url.hostname, server_url.port, modbus_url.hostname, modbus_url.port, timeout) as modbus:
        await modbus.serve_forever()


def load_log_config(file_name):
    global log
    if not file_name:
        logging.config.dictConfig(DEFAULT_LOG_CONFIG)
        return
    file_name = pathlib.Path(file_name)
    ext = file_name.suffix
    if ext.endswith('toml'):
        from toml import load
    elif ext.endswith('yml') or ext.endswith('yaml'):
        import yaml
        def load(fobj):
            return yaml.load(fobj, Loader=yaml.Loader)
    elif ext.endswith('json'):
        from json import load
    elif ext.endswith('ini') or ext.endswith('conf'):
        logging.config.fileConfig(file_name, disable_existing_loggers=False)
        return
    else:
        raise NotImplementedError
    with open(file_name) as fobj:
        obj = load(fobj)
    obj.setdefault("version", 1)
    obj.setdefault("disable_existing_loggers", False)
    logging.config.dictConfig(obj)
    log = logging.getLogger("modbus-proxy")


def main():
    parser = argparse.ArgumentParser(
        description="ModBus proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-b", "--bind", type=urlparse, default="tcp://0:5020",
        help="listen address"
    )
    parser.add_argument("--modbus", type=urlparse,
        help="modbus device address (ex: tcp://plc.acme.org:502)"
    )
    parser.add_argument("--timeout", type=float, default=10,
        help="modbus connection and request timeout in seconds"
    )
    parser.add_argument("--log-config-file", default=None, type=str,
        help="log configuration file. By default log to stderr with log level = INFO"
    )
    args = parser.parse_args()
    load_log_config(args.log_config_file)
    global log
    log = logging.getLogger("modbus-proxy")
    log.info("Starting...")
    try:
        asyncio.run(run(args.bind, args.modbus, args.timeout))
    except KeyboardInterrupt:
        log.warning("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()
