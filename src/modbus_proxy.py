# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

import argparse
import asyncio
import contextlib
import logging.config
import pathlib
import struct
import warnings
from urllib.parse import urlparse

__version__ = "0.6.8"


# Function related to data access.
READ_COILS = 1
READ_DISCRETE_INPUTS = 2
READ_HOLDING_REGISTERS = 3
READ_INPUT_REGISTERS = 4

WRITE_SINGLE_COIL = 5
WRITE_SINGLE_REGISTER = 6
WRITE_MULTIPLE_COILS = 15
WRITE_MULTIPLE_REGISTERS = 16

READ_FILE_RECORD = 20

WRITE_FILE_RECORD = 21

READ_WRITE_MULTIPLE_REGISTERS = 23
READ_FIFO_QUEUE = 24

# Diagnostic functions, only available when using serial line.
READ_EXCEPTION_STATUS = 7
DIAGNOSTICS = 8
GET_COMM_EVENT_COUNTER = 11
GET_COM_EVENT_LOG = 12
REPORT_SERVER_ID = 17

GENERAL_FUNCS = {
    READ_COILS,
    READ_DISCRETE_INPUTS,
    READ_HOLDING_REGISTERS,
    READ_INPUT_REGISTERS,
    WRITE_SINGLE_COIL,
    WRITE_SINGLE_REGISTER,
    WRITE_MULTIPLE_COILS,
    WRITE_MULTIPLE_REGISTERS,
}

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
    def __init__(self, host, port, reader=None, writer=None):
        self.host = host
        self.port = port
        self.reader = reader
        self.writer = writer
        self.log = log.getChild(self.name)

    @property
    def name(self):
        return f"{type(self).__name__}({self.host}:{self.port})"

    @classmethod
    def from_url(cls, url):
        url = parse_url(url)
        return cls(url.hostname, url.port)

    @classmethod
    def from_connection(cls, reader, writer):
        host, port = writer.get_extra_info("peername")
        return cls(host, port, reader, writer)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.close()

    @property
    def is_open(self):
        return (
            self.writer is not None
            and not self.writer.is_closing()
            and not self.reader.at_eof()
        )

    async def open(self, host=None, port=None):
        await self.close()
        if host:
            self.host = host
        if port:
            self.port = port
        self.log.info("connecting to modbus TCP at %s:%s...", self.host, self.port)
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
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

    async def write(self, data):
        self.log.debug("sending %r", data)
        self.writer.write(data)
        await self.writer.drain()

    async def read_exactly(self, n):
        self.log.debug("reading %d...", n)
        result = await self.reader.readexactly(n)
        self.log.debug("read %r", result)
        return result


class BaseProtocol:
    def __init__(self, transport):
        self.transport = transport

    async def read_request_frame(self):
        raise NotImplementedError

    async def read_response_frame(self):
        raise NotImplementedError

    async def write_frame(self, frame: bytes) -> bool:
        await self.transport.write(frame)

    async def write_read_response_frame(self, request_frame):
        await self.write_frame(request_frame)
        return await self.read_response_frame()


class TCPProtocol(BaseProtocol):
    async def read_frame(self):
        """Read ModBus TCP message"""
        header = await self.transport.read_exactly(6)
        size = int.from_bytes(header[4:], "big")
        reply = header + await self.transport.read_exactly(size)
        return reply

    read_request_frame = read_frame
    read_response_frame = read_frame


class RTUProtocol(BaseProtocol):

    REQ_DYNAMIC = {WRITE_MULTIPLE_COILS, WRITE_MULTIPLE_REGISTERS}
    REQ_STATIC = GENERAL_FUNCS - REQ_DYNAMIC

    RESP_STATIC = {
        WRITE_SINGLE_COIL,
        WRITE_SINGLE_REGISTER,
        WRITE_MULTIPLE_COILS,
        WRITE_MULTIPLE_REGISTERS,
    }
    RESP_DYNAMIC = GENERAL_FUNCS - RESP_STATIC

    async def read_request_frame(self):
        """Read ModBus RTU request from client"""
        payload = await self.transport.read_exactly(7)
        address, func, starting_address, value, byte_count = struct.unpack(
            ">BBHHB", payload
        )
        if func in self.REQ_STATIC:
            byte_count = 1  # second byte of CRC
        elif func in self.REQ_DYNAMIC:
            byte_count += 2  # CRC
        else:
            byte_count = 1  # second byte of CRC ?
            self.transport.log.warning("request: unknown modbus func code %s", func)
        # CRC is 2 bytes long
        payload += await self.transport.read_exactly(byte_count)
        return payload

    async def read_response_frame(self):
        """Read ModBus RTU response from modbus"""
        payload = await self.transport.read_exactly(2)
        address, func = struct.unpack(">BB", payload)
        if func in self.RESP_STATIC:
            byte_count = 4
        elif func in self.RESP_DYNAMIC:
            end = await self.transport.read_exactly(1)
            payload += end
            byte_count = struct.unpack(">B", end)[0]
        elif func & 0x80:  # an error
            byte_count = 1
        else:
            byte_count = 0
            self.transport.log.warning("response: unknown modbus func code %s", func)

        # CRC is 2 bytes long
        payload += await self.transport.read_exactly(byte_count + 2)
        return payload


def transport_protocol_for_url(url):
    url_parsed = parse_url(url)
    scheme = url_parsed.scheme
    if not scheme:
        scheme = "tcp"
    if "+" in scheme:
        transport, protocol = scheme.rsplit("+", 1)
    elif scheme == "tcp":
        transport, protocol = "tcp", "tcp"
    else:
        transport, protocol = scheme, "rtu"
    return transport, protocol


def modbus_for_url(url):
    transport_name, protocol_name = transport_protocol_for_url(url)
    if transport_name == "tcp":
        transport = TCP.from_url(url)
    else:
        import serialio

        transport = serialio.serial_for_url(url)
        transport.read_exactly = transport.read
    if protocol_name == "tcp":
        protocol = TCPProtocol(transport)
    elif protocol_name == "rtu":
        protocol = RTUProtocol(transport)
    else:
        raise ValueError(f"uknnown protocol for {url!r}")
    return transport, protocol


class Bridge:
    def __init__(self, config):
        modbus = config["modbus"]
        url = modbus["url"]
        bind = config["listen"]["bind"]
        self.log = log.getChild(f"Bridge({bind} <-> {url})")
        self.config = config
        bind = parse_url(bind)
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
    def is_open(self):
        return self.transport.is_open

    async def open(self):
        await asyncio.wait_for(self.transport.open(), self.timeout)
        if self.connection_time > 0:
            self.log.info("delay after connect: %s", self.connection_time)
            await asyncio.sleep(self.connection_time)

    async def write_read_response_frame(self, request_frame, attempts=2):
        async with self.lock:
            for i in range(attempts):
                try:
                    if not self.is_open:
                        await self.open()
                    coro = self.protocol.write_read_response_frame(request_frame)
                    return await asyncio.wait_for(coro, self.timeout)
                except Exception as error:
                    await self.close()
                    if i + 1 == attempts:
                        raise
                    self.log.error(
                        "write_read error [%s/%s]: %r", i + 1, attempts, error
                    )

    async def handle_client_message(self, client):
        request_frame = await client.read_request_frame()
        reply = await self.write_read_response_frame(request_frame)
        await client.write_frame(reply)

    async def handle_client(self, reader, writer):
        async with TCP.from_connection(reader, writer) as transport:
            protocol = type(self.protocol)(transport)
            while True:
                try:
                    await self.handle_client_message(protocol)
                except asyncio.IncompleteReadError as error:
                    if error.partial:
                        transport.log.error("reading error: %r", error)
                    else:
                        transport.log.info("client closed connection")
                    await transport.close()
                    break
                except Exception as error:
                    transport.log.error("reading error: %r", error)
                    await transport.close()
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
