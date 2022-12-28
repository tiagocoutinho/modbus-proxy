# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.


import asyncio
import pathlib
import argparse
import warnings
import contextlib
import logging.config
from urllib.parse import urlparse

__version__ = "0.6.8"


DEFAULT_LOG_CONFIG = {
    "version": 1,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)8s %(name)s: %(message)s"}
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"}
    },
    "root": {"handlers": ["console"], "level": "INFO"},
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


class TCP:
    def __init__(self, name, reader=None, writer=None):
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
        return (
            self.writer is not None
            and not self.writer.is_closing()
            and not self.reader.at_eof()
        )

    async def connect(self, host, port):
        await self.close()
        self.log.info("connecting to modbus TCP at %s:%s...", host, port)
        self.reader, self.writer = await asyncio.open_connection(host, port)
        self.log.info("connected!")

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

    async def raw_write(self, data):
        self.log.debug("sending %r", data)
        self.writer.write(data)
        await self.writer.drain()

    async def write(self, data):
        try:
            await self.raw_write(data)
        except Exception as error:
            self.log.error("writting error: %r", error)
            await self.close()
            return False
        return True

    async def read_exactly(self, n):
        return await self.reader.readexactly(n)

    async def raw_read(self):
        """Read ModBus TCP message"""
        # TODO: Handle Modbus RTU and ASCII
        header = await self.reader.readexactly(6)
        size = int.from_bytes(header[4:], "big")
        reply = header + await self.reader.readexactly(size)
        self.log.debug("received %r", reply)
        return reply

    async def read(self):
        try:
            return await self.raw_read()
        except asyncio.IncompleteReadError as error:
            if error.partial:
                self.log.error("reading error: %r", error)
            else:
                self.log.info("client closed connection")
            await self.close()
        except Exception as error:
            self.log.error("reading error: %r", error)
            await self.close()

    async def raw_write_read(self, data):
        await self.raw_write(data)
        return await self.raw_read()


class Client(TCP):
    def __init__(self, reader, writer):
        peer = writer.get_extra_info("peername")
        super().__init__(f"Client({peer[0]}:{peer[1]})", reader, writer)
        self.log.info("new client connection")


class BaseProtocol:

    def __init__(self, transport):
        self.transport = transport

    async def write_read(self, data):
        await self.transport.write(data)
        return await self.read()

    async def read(self):
        raise NotImplementedError


class TCPProtocol(BaseProtocol):

    async def read(self):
        """Read ModBus TCP message"""
        header = await self.transport.read_exactly(6)
        size = int.from_bytes(header[4:], "big")
        reply = header + await self.transport.read_exactly(size)
        return reply


class ModBusTCP:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.tcp = TCP(f"TCP({self.host}:{self.port})")

    async def connect(self):
        await self.tcp.connect(self.host, self.port)

    @property
    def opened(self):
        return self.tcp.opened

    async def close(self):
        await self.tcp.close()

    async def write(self, data):
        await self.tcp.write(data)

    async def read_exactly(self, n):
        return await self.tcp.read_exactly(n)

    async def write_read(self, data):
        return await self.tcp.raw_write_read(data)



def modbus_for_url(url):
    url = parse_url(url)
    if url.scheme == "tcp":
        transport = ModBusTCP(url.hostname, url.port)
        protocol = TCPProtocol(transport)
    return transport, protocol


class Bridge:
    def __init__(self, config):
        modbus = config["modbus"]
        url = modbus["url"]
        bind = parse_url(config["listen"]["bind"])
        self.log = log.getChild(f"Bridge({bind} <-> {url})")
        self.transport, self.protocol = modbus_for_url(url)
        self.host = bind.hostname
        self.port = 502 if bind.port is None else bind.port
        self.timeout = modbus.get("timeout", None)
        self.connection_time = modbus.get("connection_time", 0)
        self.server = None
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.close()

    async def close(self):
        await self.transport.close()

    @property
    def address(self):
        if self.server is not None:
            return self.server.sockets[0].getsockname()

    @property
    def opened(self):
        return self.transport.opened

    async def connect(self):
        if not self.opened:
            await asyncio.wait_for(self.transport.connect(), self.timeout)
            if self.connection_time > 0:
                self.log.info("delay after connect: %s", self.connection_time)
                await asyncio.sleep(self.connection_time)

    async def write_read(self, data, attempts=2):
        async with self.lock:
            for i in range(attempts):
                try:
                    await self.connect()
                    coro = self.protocol.write_read(data)
                    return await asyncio.wait_for(coro, self.timeout)
                except Exception as error:
                    self.log.error(
                        "write_read error [%s/%s]: %r", i + 1, attempts, error
                    )
                    await self.close()

    async def handle_client(self, reader, writer):
        async with Client(reader, writer) as client:
            while True:
                request = await client.read()
                if not request:
                    break
                reply = await self.write_read(request)
                if not reply:
                    break
                result = await client.write(reply)
                if not result:
                    break

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port, start_serving=True
        )

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        await self.close()

    async def serve_forever(self):
        if self.server is None:
            await self.start()
        async with self.server:
            self.log.info("Ready to accept requests on %s:%d", self.host, self.port)
            await self.server.serve_forever()


def load_config(file_name):
    file_name = pathlib.Path(file_name)
    ext = file_name.suffix
    if ext.endswith("toml"):
        from toml import load
    elif ext.endswith("yml") or ext.endswith("yaml"):
        import yaml

        def load(fobj):
            return yaml.load(fobj, Loader=yaml.Loader)

    elif ext.endswith("json"):
        from json import load
    else:
        raise NotImplementedError
    with open(file_name) as fobj:
        return load(fobj)


def prepare_log(config, log_config_file=None):
    cfg = config.get("logging")
    if not cfg:
        if log_config_file:
            if log_config_file.endswith("ini") or log_config_file.endswith("conf"):
                logging.config.fileConfig(
                    log_config_file, disable_existing_loggers=False
                )
            else:
                cfg = load_config(log_config_file)
        else:
            cfg = DEFAULT_LOG_CONFIG
    if cfg:
        cfg.setdefault("version", 1)
        cfg.setdefault("disable_existing_loggers", False)
        logging.config.dictConfig(cfg)
    warnings.simplefilter("always", DeprecationWarning)
    logging.captureWarnings(True)
    if log_config_file:
        warnings.warn(
            "log-config-file deprecated. Use config-file instead", DeprecationWarning
        )
        if "logging" in config:
            log.warning("log-config-file ignored. Using config file logging")
    return log


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="ModBus proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config-file", default=None, type=str, help="config file"
    )
    parser.add_argument("-b", "--bind", default=None, type=str, help="listen address")
    parser.add_argument(
        "--modbus",
        default=None,
        type=str,
        help="modbus device address (ex: tcp://plc.acme.org:502)",
    )
    parser.add_argument(
        "--modbus-connection-time",
        type=float,
        default=0,
        help="delay after establishing connection with modbus before first request",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="modbus connection and request timeout in seconds",
    )
    parser.add_argument(
        "--log-config-file",
        default=None,
        type=str,
        help="log configuration file. By default log to stderr with log level = INFO",
    )
    options = parser.parse_args(args=args)

    if not options.config_file and not options.modbus:
        parser.exit(1, "must give a config-file or/and a --modbus")
    return options


def create_config(args):
    if args.config_file is None:
        assert args.modbus
    config = load_config(args.config_file) if args.config_file else {}
    prepare_log(config, args.log_config_file)
    log.info("Starting...")
    devices = config.setdefault("devices", [])
    if args.modbus:
        listen = {"bind": ":502" if args.bind is None else args.bind}
        devices.append(
            {
                "modbus": {
                    "url": args.modbus,
                    "timeout": args.timeout,
                    "connection_time": args.modbus_connection_time,
                },
                "listen": listen,
            }
        )
    return config


def create_bridges(config):
    return [Bridge(cfg) for cfg in config["devices"]]


async def start_bridges(bridges):
    coros = [bridge.start() for bridge in bridges]
    await asyncio.gather(*coros)


async def run_bridges(bridges, ready=None):
    async with contextlib.AsyncExitStack() as stack:
        coros = [stack.enter_async_context(bridge) for bridge in bridges]
        await asyncio.gather(*coros)
        await start_bridges(bridges)
        if ready is not None:
            ready.set(bridges)
        coros = [bridge.serve_forever() for bridge in bridges]
        await asyncio.gather(*coros)


async def run(args=None, ready=None):
    args = parse_args(args)
    config = create_config(args)
    bridges = create_bridges(config)
    await run_bridges(bridges, ready=ready)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.warning("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()
