# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2025 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

import argparse
import asyncio
import logging.config
import pathlib
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


class Stream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.remote_address = self.writer.get_extra_info("peername")

    async def write(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def close(self) -> None:
        """Closes the connection."""
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError as error:
            host, port = self.remote_address
            log.info("Failed to disconnect to %s:%d: %r", host, port, error)

    async def read_message(self) -> bytes:
        """Read ModBus TCP message"""
        header = await self.reader.readexactly(6)
        size = int.from_bytes(header[4:], "big")
        message = header + await self.reader.readexactly(size)
        return message


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
        self.unit_id_map = config.get("unit_id_remapping") or {}
        self.unit_id_map_reverse = {v: k for k, v in self.unit_id_map.items()}
        self.clients = set()
        self.device = None
        self.to_device_queue = asyncio.Queue(maxsize=1000)
        self.to_client_queue = asyncio.Queue(maxsize=1000)
        self.log = log.getChild(f"{self.device_host}:{self.device_port}")

    def parse_request(self, request) -> Buffer:
        uid = request[6]
        if (new_uid := self.unit_id_map.get(uid)) is not None:
            request = bytearray(request)
            request[6] = new_uid
        return request

    def _transform_reply(self, reply) -> Buffer:
        uid = reply[6]
        if (new_uid := self.unit_id_map_reverse.get(uid)) is not None:
            reply = bytearray(reply)
            reply[6] = new_uid
        return reply

    async def try_read_message(self, reader) -> Buffer | None:
        try:
            message = await reader.read_message()
            return self.parse_request(message)
        except asyncio.IncompleteReadError as error:
            if error.partial:
                self.log.error("Reading error: %r", error)
            return
        except Exception as error:
            self.log.error("Reading error: %r", error)
            return

    async def reconnect_to_device(self):
        device = self.device
        if device is not None:
            await device.close()
            self.device = None
        coro = asyncio.open_connection(self.device_host, self.device_port)
        device = Stream(*await asyncio.wait_for(coro, timeout=self.timeout))
        if self.connection_time > 0:
            self.log.info(
                "delay after connect %ss for %s",
                self.connection_time,
                device.remote_address,
            )
            await asyncio.sleep(self.connection_time)
        self.device = device
        self.log.info("Connected to %s", self.device.remote_address)

    async def write_to_device(self, message):
        if self.device is None:
            await self.reconnect_to_device()
            await self.device.write(message)
        else:
            try:
                await self.device.write(message)
            except OSError:
                await self.reconnect_to_device()
                await self.device.write(message)

    async def to_modbus(self):
        while True:
            message, client = await self.to_device_queue.get()
            if message is None:
                if not self.clients and self.device is not None:
                    log.info("Closing connection to device")
                    await self.device.close()
                    self.device = None
                continue
            try:
                await self.write_to_device(message)
                await self.to_client_queue.put(client)
            except OSError:
                await client.close()

    async def to_client(self):
        while True:
            client = await self.to_client_queue.get()
            reply = await self.try_read_message(self.device)
            if reply is None:
                await client.close()
            else:
                reply = self._transform_reply(reply)
                await client.write(reply)

    async def handle_client(self, reader, writer):
        client = Stream(reader, writer)
        self.log.info("Client connected from %s", client.remote_address)
        self.clients.add(client)
        try:
            while True:
                message = await self.try_read_message(client)
                if message is None:
                    break
                await self.to_device_queue.put((message, client))
        finally:
            self.log.info("Client at %s disconnected", client.remote_address)
            self.clients.discard(client)
            await self.to_device_queue.put((None, client))

    async def serve_forever(self):
        url = self.device_url.geturl()
        await asyncio.start_server(
            self.handle_client, self.server_host, self.server_port
        )
        self.log.info("Ready to accept requests")
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.to_modbus(), name=f"client->modbus {url}")
            tg.create_task(self.to_client(), name=f"modbus->client {url}")


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
