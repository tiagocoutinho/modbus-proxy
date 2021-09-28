import asyncio
import pathlib
import argparse
import warnings
import contextlib
import logging.config
from urllib.parse import urlparse

__version__ = "0.5.0"


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

log = logging.getLogger("modbus-proxy")


def parse_url(url):
    if "://" not in url:
        url = f"tcp://{url}"
    result = urlparse(url)
    if not result.hostname:
        url = result.geturl().replace("://", "://0")
        result = urlparse(url)
    return result


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

    def __init__(self, config):
        modbus, bind = config["modbus"], config["listen"]["bind"]
        url = modbus["url"]
        super().__init__(f"ModBus({url.hostname}:{url.port})", None, None)
        self.host = bind.hostname
        self.port = bind.port or 502
        self.modbus_host = url.hostname
        self.modbus_port = url.port
        self.timeout = modbus.get("timeout", None)
        self.connection_time = modbus.get("connection_time", 0)
        self.lock = asyncio.Lock()

    async def open(self):
        self.log.info("connecting to modbus...")
        self.reader, self.writer = \
            await asyncio.open_connection(self.modbus_host, self.modbus_port)
        self.log.info("connected!")

    async def connect(self):
        if not self.opened:
            await asyncio.wait_for(self.open(), self.timeout)
            if self.connection_time > 0:
                self.log.info("delay after connect: %s", self.connection_time)
                await asyncio.sleep(self.connection_time)

    async def write_read(self, data, attempts=2):
        async with self.lock:
            for i in range(attempts):
                try:
                    await self.connect()
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
        async with self:
            server = await asyncio.start_server(
                self.handle_client, self.host, self.port
            )
            async with server:
                self.log.info("Ready to accept requests on %s:%d", self.host, self.port)
                await server.serve_forever()


def load_config(file_name):
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
    else:
        raise NotImplementedError
    with open(file_name) as fobj:
        return load(fobj)


def prepare_log(config, log_config_file=None):
    cfg = config.get("logging")
    if not cfg:
        if log_config_file:
            if log_config_file.endswith('ini') or log_config_file.endswith('conf'):
                logging.config.fileConfig(log_config_file, disable_existing_loggers=False)
            else:
                cfg = load_config(log_config_file)
        else:
            cfg = DEFAULT_LOG_CONFIG
    if cfg:
        cfg.setdefault("version", 1)
        cfg.setdefault("disable_existing_loggers", False)
        logging.config.dictConfig(cfg)
    warnings.simplefilter('always', DeprecationWarning)
    logging.captureWarnings(True)
    if log_config_file:
        warnings.warn("log-config-file deprecated. Use config-file instead", DeprecationWarning)
        if "logging" in config:
            log.warning("log-config-file ignored. Using config file logging")
    return log


async def run(args):
    if args.config_file is None:
        assert args.modbus
    config = load_config(args.config_file) if args.config_file else {}
    prepare_log(config, args.log_config_file)
    log.info("Starting...")
    devices = config.get("devices", [])
    if args.modbus:
        listen = {"bind": ":502" if args.bind is None else args.bind}
        devices.append({
            "modbus": {
                "url": args.modbus,
                "timeout": args.timeout,
                "connection_time": args.modbus_connection_time
            },
            "listen": listen
        })
    # transport url strings in Url objects
    for device in devices:
        modbus, listen = device["modbus"], device["listen"]
        modbus["url"] = parse_url(modbus["url"])
        listen["bind"] = parse_url(listen["bind"])
    async with contextlib.AsyncExitStack() as stack:
        servers = [await stack.enter_async_context(ModBus(cfg)) for cfg in devices]
        coros = [server.serve_forever() for server in servers]
        await asyncio.gather(*coros)


def main():
    parser = argparse.ArgumentParser(
        description="ModBus proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "-c", "--config-file", default=None, type=str, help="config file"
    )
    parser.add_argument(
        "-b", "--bind", default=None, type=str, help="listen address"
    )
    parser.add_argument("--modbus", default=None, type=str,
        help="modbus device address (ex: tcp://plc.acme.org:502)"
    )
    parser.add_argument("--modbus-connection-time", type=float, default=0.1,
        help="delay after establishing connection with modbus before first request"
    )
    parser.add_argument("--timeout", type=float, default=10,
        help="modbus connection and request timeout in seconds"
    )
    parser.add_argument("--log-config-file", default=None, type=str,
        help="log configuration file. By default log to stderr with log level = INFO"
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.warning("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()
