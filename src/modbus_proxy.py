# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2025 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

import argparse
import asyncio
import collections
import logging.config
import pathlib
from typing import Self
import urllib.parse

from collections.abc import Buffer

__version__ = "1.0.0"


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


log: logging.Logger = logging.getLogger("modbus-proxy")


def parse_url(url: str) -> urllib.parse.ParseResult:
    if "://" not in url:
        url = f"tcp://{url}"
    result = urllib.parse.urlparse(url)
    if not result.hostname:
        url = result.geturl().replace("://", "://0")
        result = urllib.parse.urlparse(url)
    return result


class Result(collections.namedtuple("Result", "type value")):
    ERROR = 0
    OK = 1

    """Execution result. Similar to Rust result"""

    def is_ok(self):
        return self.type == self.OK

    def is_err(self):
        return self.type == self.ERROR

    def __bool__(self):
        return self.is_ok()

    @classmethod
    def Ok(cls, value=None):
        return cls(cls.OK, value)
    
    @classmethod
    def Err(cls, error):
        return cls(cls.ERROR, error)


class Stream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.remote_address = host, port = self.writer.get_extra_info("peername")
        self.log = log.getChild(f"{host}:{port}")
        self.log.info("Connected!")

    async def close(self) -> Result:
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError as error:
            log.info("failed to close: %r", error)
            return Result.Err(error)
        return Result.Ok()

    async def read_message(self) -> Result:
        try:
            header = await self.reader.readexactly(6)
            size = int.from_bytes(header[4:], "big")
            result = bytearray(6 + size)
            result[:6] = header
            result[6:] = await self.reader.readexactly(size)
            return Result.Ok(result)
        except asyncio.IncompleteReadError as error:
            if n := len(error.partial):
                log.warning(" after sending partial %d bytes", n)
            else:
                log.info("client disconnected")
        except OSError as error:
            log.info("failed to read: %r", error)
            return Result.Err(error)

    async def write(self, payload):
        self.writer.write(payload)
        await self.writer.drain()        

    async def write_message(self, payload) -> Result:
        try:
            await self.write(payload)
            return Result.Ok()
        except OSError as error:
            log.info("failed to write: %r", error)
            return Result.Err(error)


class Bridge:
    def __init__(self, config):
        bind = parse_url(config["listen"]["bind"])
        self.server_host = bind.hostname
        self.server_port = 502 if bind.port is None else bind.port
        modbus = config["modbus"]
        self.device_url = parse_url(modbus["url"])
        self.device_host = self.device_url.hostname
        self.device_port = self.device_url.port
        self.timeout = modbus.get("timeout", None)
        self.connection_time = modbus.get("connection_time", 0)
        self.unit_id_map = config.get("unit_id_map", config.get("unit_id_remapping", {}))
        self.unit_id_map_reverse = {v: k for k, v in self.unit_id_map.items()}
        self.device = None
        self.to_device_queue = asyncio.Queue(maxsize=1000)
        self.to_client_queue = asyncio.Queue(maxsize=1000)

    def parse_request(self, request: bytearray) -> bytearray:
        uid = request[6]
        if (new_uid := self.unit_id_map.get(uid)) is not None:
            request[6] = new_uid
        return request

    def parse_reply(self, reply: bytearray) -> bytearray:
        uid = reply[6]
        if (new_uid := self.unit_id_map_reverse.get(uid)) is not None:
            reply[6] = new_uid
        return reply

    async def reconnect(self):
        device = self.device
        if device is not None:
            await device.close()
            self.device = None
        coro = asyncio.open_connection(self.device_host, self.device_port)
        if self.connection_time > 0:
            await asyncio.sleep(self.connection_time)
        self.device = Stream(*await asyncio.wait_for(coro, timeout=self.timeout))

    async def write_to_device(self, message):
        try:
            if self.device is None:
                await self.reconnect()
                await self.device.write(message)
            else:
                try:
                    await self.device.write(message)
                except OSError:
                    await self.reconnect()
                    await self.device.write(message)
            return Result.Ok()
        except Exception as error:
            return Result.Err(error)

    async def to_device_loop(self):
        while True:
            message, client = await self.to_device_queue.get()
            if await self.write_to_device(message):
                await self.to_client_queue.put(client)
            else:
                await client.close()

    async def to_client_loop(self):
        while True:
            client = await self.to_client_queue.get()
            if reply := await self.device.read_message():
                reply = self.parse_reply(reply.value)
                if not await client.write_message(reply):
                    await client.close()
            else:
                await client.close()

    async def handle_client(self, reader, writer):
        client = Stream(reader, writer)
        while True:
            if not (message := await client.read_message()):
                break
            message = self.parse_request(message.value)
            await self.to_device_queue.put((message, client))

    async def serve_forever(self):
        url = self.device_url.geturl()
        await asyncio.start_server(
            self.handle_client, self.server_host, self.server_port
        )
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.to_device_loop(), name=f"client->modbus {url}")
            tg.create_task(self.to_client_loop(), name=f"modbus->client {url}")


def create_bridges(config):
    return [Bridge(device) for device in config["devices"]]


def load_config(file_name):
    file_name = pathlib.Path(file_name)
    ext = file_name.suffix
    if ext.endswith("toml"):
        from tomllib import load
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


def prepare_log(config):
    cfg = config.get("logging")
    if not cfg:
        cfg = DEFAULT_LOG_CONFIG
    if cfg:
        cfg.setdefault("version", 1)
        cfg.setdefault("disable_existing_loggers", False)
        logging.config.dictConfig(cfg)
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
    options = parser.parse_args(args=args)

    if not options.config_file and not options.modbus:
        parser.exit(1, "must give a config-file or/and a --modbus")
    return options


def create_config(args):
    if args.config_file is None:
        assert args.modbus
    config = load_config(args.config_file) if args.config_file else {}
    prepare_log(config)
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


async def run(args=None, ready=None):
    args = parse_args(args)
    config = create_config(args)
    bridges = create_bridges(config)
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(bridge.serve_forever()) for bridge in bridges]


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.warning("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()
