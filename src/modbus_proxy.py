# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.


import time
import asyncio
import pathlib
import argparse
import warnings
import contextlib
import logging.config
from urllib.parse import urlparse

__version__ = "0.8.1-beta3"


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
        return (
            self.writer is not None
            and not self.writer.is_closing()
            and not self.reader.at_eof()
        )

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
            self.log.error("writing error: %r", error)
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
        modbus = config["modbus"]
        url = parse_url(modbus["url"])
        bind = parse_url(config["listen"]["bind"])
        super().__init__(f"ModBus({url.hostname}:{url.port})", None, None)
        self.host = bind.hostname
        self.port = 502 if bind.port is None else bind.port
        self.modbus_host = url.hostname
        self.modbus_port = url.port
        self.timeout = modbus.get("timeout", None)
        self.connection_time = modbus.get("connection_time", 0)
        self.reconnect_delay = modbus.get("reconnect_delay", 0)
        self.unit_id_remapping = config.get("unit_id_remapping") or {}
        self.server = None
        self.lock = asyncio.Lock()
        self.idle_time = modbus.get("idle_time", 0)
        self.last_activity_ts = float("Inf")
        self.idle_tracker_task = None
        self.request_delay_ns = modbus.get("request_delay", 0) * 1e9  # sec to ns
        self.last_request_ts_ns = 0  # timestamp in nanoseconds
        self.connection_ttl = modbus.get("connection_ttl", 0)
        self.ttl_monitor_task = None

    def _activity(method):
        """Decorator for methods that interact with remote modbus devices."""
        async def wrapper(self, *args, **kwargs):
            self.log.debug("activity started")
            # prevent self.idle_tracker_task to close the connection while
            # an activity is running
            self.last_activity_ts = float("Inf")
            try:
                return await method(self, *args, **kwargs)
            finally:
                # update last activity timestamp
                self.last_activity_ts = time.time()
                self.log.debug("activity ended")
        return wrapper

    async def _idle_tracker(self):
        """Close modbus connection if it remains idle."""
        self.log.info(
            "starting idle tracker with %d seconds of max idle time",
            self.idle_time
        )
        while (current_ts := time.time()) - self.last_activity_ts < self.idle_time:
            await asyncio.sleep(
                min(
                    self.last_activity_ts + self.idle_time - current_ts,
                    self.idle_time
                ) + 1
            )
            self.log.debug("idle tracker check")
        self.log.info("idle tracker timed out")
        await self.close(_idle=True)

    async def _ttl_monitor(self):
        """Close modbus connection after `self.connection_ttl` seconds since opening."""
        self.log.info(
            "starting connection monitor with %d seconds ttl",
            self.connection_ttl
        )
        await asyncio.sleep(self.connection_ttl)
        if self.opened:
            self.log.info("connection ttl reached")
            async with self.lock:
                await self.close(_ttl=True)

    @property
    def address(self):
        if self.server is not None:
            return self.server.sockets[0].getsockname()

    async def close(self, _idle=False, _ttl=False):
        if not _idle and self.idle_tracker_task:
            self.idle_tracker_task.cancel()
            self.idle_tracker_task = None
        if not _ttl and self.ttl_monitor_task:
            self.ttl_monitor_task.cancel()
            self.ttl_monitor_task = None
        await super().close()

    @_activity
    async def open(self):
        self.log.info("connecting to modbus...")
        self.reader, self.writer = await asyncio.open_connection(
            self.modbus_host, self.modbus_port
        )
        self.log.info("connected!")

    async def connect(self):
        if not self.opened:
            await asyncio.wait_for(self.open(), self.timeout)
            if self.connection_time > 0:
                self.log.info("delay after connect: %s", self.connection_time)
                await asyncio.sleep(self.connection_time)
            if self.idle_time > 0:
                self.idle_tracker_task = asyncio.create_task(self._idle_tracker())
            if self.connection_ttl > 0:
                self.ttl_monitor_task = asyncio.create_task(self._ttl_monitor())

    @_activity
    async def write_read(self, data, attempts=2):
        async with self.lock:
            for i in range(attempts):
                try:
                    await self.connect()
                    if time.time_ns() - self.last_request_ts_ns < self.request_delay_ns:
                        self.log.debug("delaying request")
                        await asyncio.sleep(self.request_delay_ns / 1e9)  # nano to sec
                    coro = self._write_read(data)
                    result = await asyncio.wait_for(coro, self.timeout)
                except Exception as error:
                    self.log.error(
                        "write_read error [%s/%s]: %r", i + 1, attempts, error
                    )
                    await self.close()
                    if self.reconnect_delay > 0:
                        await asyncio.sleep(self.reconnect_delay)
                else:
                    self.last_request_ts_ns = time.time_ns()
                    return result

    async def _write_read(self, data):
        await self._write(data)
        return await self._read()

    def _transform_request(self, request):
        uid = request[6]
        new_uid = self.unit_id_remapping.setdefault(uid, uid)
        if uid != new_uid:
            request = bytearray(request)
            request[6] = new_uid
            self.log.debug("remapping unit ID %s to %s in request", uid, new_uid)
        return request

    def _transform_reply(self, reply):
        uid = reply[6]
        inverse_unit_id_map = {v: k for k, v in self.unit_id_remapping.items()}
        new_uid = inverse_unit_id_map.setdefault(uid, uid)
        if uid != new_uid:
            reply = bytearray(reply)
            reply[6] = new_uid
            self.log.debug("remapping unit ID %s to %s in reply", uid, new_uid)
        return reply

    async def handle_client(self, reader, writer):
        async with Client(reader, writer) as client:
            while True:
                request = await client.read()
                if not request:
                    break
                reply = await self.write_read(self._transform_request(request))
                if not reply:
                    break
                result = await client.write(self._transform_reply(reply))
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


def prepare_log(config):
    cfg = config.get("logging")
    if not cfg:
        cfg = DEFAULT_LOG_CONFIG
    if cfg:
        cfg.setdefault("version", 1)
        cfg.setdefault("disable_existing_loggers", False)
        logging.config.dictConfig(cfg)
    warnings.simplefilter("always", DeprecationWarning)
    logging.captureWarnings(True)
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
        "--modbus-connection-ttl",
        type=float,
        default=0,
        help="max duration in seconds of a connection to modbus "
             "device (disable with 0)",
    )
    parser.add_argument(
        "--modbus-idle-time",
        type=float,
        default=0,
        help="max idle time in seconds before closing modbus "
             "connection (disable with 0)",
    )
    parser.add_argument(
        "--modbus-reconnect-delay",
        type=float,
        default=0,
        help="delay in seconds before establishing a new connection to modbus "
             "device after an error",
    )
    parser.add_argument(
        "--modbus-request-delay",
        type=float,
        default=0,
        help="minimum time to wait, in seconds, between two sequential requests",
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
                    "connection_ttl": args.modbus_connection_ttl,
                    "idle_time": args.modbus_idle_time,
                    "reconnect_delay": args.modbus_reconnect_delay,
                    "request_delay": args.modbus_request_delay,
                },
                "listen": listen,
            }
        )
    return config


def create_bridges(config):
    return [ModBus(cfg) for cfg in config["devices"]]


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
