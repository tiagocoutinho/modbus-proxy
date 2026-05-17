# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.


import asyncio
import pathlib
import argparse
import struct
import warnings
import contextlib
import logging.config
from urllib.parse import urlparse

__version__ = "0.8.0"


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


# ---------------------------------------------------------------------------
# SunSpec register conversion helpers
# ---------------------------------------------------------------------------

def _decode_int16(data, offset):
    return struct.unpack_from(">h", data, offset)[0]


def _encode_float32(value):
    return struct.pack(">f", value)


def _decode_float32(data, offset):
    return struct.unpack_from(">f", data, offset)[0]


def _encode_int16_sf(value):
    """Encode a float as int16 + scale factor, maximising precision."""
    if value == 0:
        return struct.pack(">hh", 0, 0)
    for sf in range(-10, 1):
        scaled = round(value * (10 ** -sf))
        if -32768 <= scaled <= 32767:
            return struct.pack(">hh", int(scaled), sf)
    scaled = max(-32767, min(32767, int(value)))
    return struct.pack(">hh", scaled, 0)


class RegisterConversion:
    """
    One SunSpec register conversion rule.

    source_type / target_type: "int16" or "float32"

    When source_type is "int16", sf_address (base-1) is required so the
    proxy can read the SunSpec scale factor and compute the real value.
    """

    VALID_TYPES = {"int16", "float32"}

    def __init__(self, address, source_type, target_type, sf_address=None):
        if source_type not in self.VALID_TYPES:
            raise ValueError(f"source_type must be one of {self.VALID_TYPES}")
        if target_type not in self.VALID_TYPES:
            raise ValueError(f"target_type must be one of {self.VALID_TYPES}")
        if source_type == target_type:
            raise ValueError("source_type and target_type must differ")
        if source_type == "int16" and sf_address is None:
            raise ValueError("sf_address is required when source_type is int16")
        self.address = address - 1
        self.sf_address = (sf_address - 1) if sf_address is not None else None
        self.source_type = source_type
        self.target_type = target_type

    @classmethod
    def from_config(cls, cfg):
        return cls(
            address=cfg["address"],
            source_type=cfg["source_type"],
            target_type=cfg["target_type"],
            sf_address=cfg.get("sf_address"),
        )

    def __repr__(self):
        return (
            f"RegisterConversion(address={self.address + 1}, "
            f"{self.source_type}→{self.target_type})"
        )


class SunSpecConverter:
    """
    Applies RegisterConversion rules to Modbus TCP reply payloads.
    Scale factor values are cached from previous replies.
    """

    def __init__(self, conversions):
        self.conversions = conversions
        self._sf_cache = {}

    def _parse_read_response(self, reply):
        if len(reply) < 9:
            return None
        if reply[7] != 0x03:
            return None
        byte_count = reply[8]
        if len(reply) < 9 + byte_count:
            return None
        return bytearray(reply[9:9 + byte_count]), byte_count // 2

    def _parse_read_request(self, request):
        if len(request) < 12:
            return None
        if request[7] != 0x03:
            return None
        start = int.from_bytes(request[8:10], "big")
        count = int.from_bytes(request[10:12], "big")
        return start, count

    def update_sf_cache(self, request, reply):
        req = self._parse_read_request(request)
        res = self._parse_read_response(reply)
        if req is None or res is None:
            return
        start, count = req
        data, _ = res
        for conv in self.conversions:
            if conv.sf_address is None:
                continue
            if start <= conv.sf_address < start + count:
                offset = (conv.sf_address - start) * 2
                self._sf_cache[conv.sf_address] = _decode_int16(data, offset)

    async def warmup(self, write_read_func):
        """
        Read all configured scale factor registers at startup to populate
        the cache before the first client request arrives.

        This prevents the first poll from returning incorrect values when
        the SF register is not included in the client's polling window.

        Reads each SF register individually to avoid exceeding the Modbus
        maximum of 125 registers per request.
        """
        sf_addresses = {
            conv.sf_address
            for conv in self.conversions
            if conv.sf_address is not None
        }
        if not sf_addresses:
            return

        cached = []
        for sf_addr in sorted(sf_addresses):
            # Build a single-register read request for each SF
            request = (
                b"\x00\x01"  # transaction id
                b"\x00\x00"  # protocol id
                b"\x00\x06"  # length
                b"\x01"      # unit id
                b"\x03"      # function code: read holding registers
                + sf_addr.to_bytes(2, "big")
                + b"\x00\x01"  # count: 1 register
            )
            reply = None
            for attempt in range(5):
                reply = await write_read_func(request)
                if reply is not None:
                    break
                log.info(
                    "SF warmup retry %d/5 for register %d in 3s...",
                    attempt + 1,
                    sf_addr + 1,
                )
                await asyncio.sleep(3)

            if reply is None:
                log.warning(
                    "SF warmup failed for register %d after 5 attempts — "
                    "cache will be populated on first poll",
                    sf_addr + 1,
                )
                continue
            self.update_sf_cache(request, reply)
            cached.append(sf_addr + 1)

        if cached:
            log.info(
                "SF warmup complete — cached %d scale factor register(s): %s",
                len(cached),
                cached,
            )

    def transform_reply(self, request, reply):
        req = self._parse_read_request(request)
        res = self._parse_read_response(reply)
        if req is None or res is None:
            return reply

        start, count = req
        data, _ = res
        modified = False

        for conv in self.conversions:
            if not (start <= conv.address < start + count):
                continue
            offset = (conv.address - start) * 2

            if conv.source_type == "int16" and conv.target_type == "float32":
                sf = self._sf_cache.get(conv.sf_address)
                if sf is None:
                    if start <= conv.sf_address < start + count:
                        sf = _decode_int16(data, (conv.sf_address - start) * 2)
                        self._sf_cache[conv.sf_address] = sf
                    else:
                        continue
                raw_int = _decode_int16(data, offset)
                encoded = _encode_float32(float(raw_int) * (10.0 ** sf))
                data[offset:offset + 2] = encoded[0:2]
                if start <= conv.sf_address < start + count:
                    data[(conv.sf_address - start) * 2:(conv.sf_address - start) * 2 + 2] = encoded[2:4]
                modified = True

            elif conv.source_type == "float32" and conv.target_type == "int16":
                if offset + 4 > len(data):
                    continue
                data[offset:offset + 4] = _encode_int16_sf(_decode_float32(data, offset))
                modified = True

        if not modified:
            return reply
        reply = bytearray(reply)
        reply[9:9 + len(data)] = data
        return bytes(reply)


# ---------------------------------------------------------------------------
# Connection base class
# ---------------------------------------------------------------------------

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
            self.log.error("writting error: %r", error)
            await self.close()
            return False
        return True

    async def _read(self):
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


# ---------------------------------------------------------------------------
# Listener — one TCP server endpoint, with optional SunSpec conversion
# ---------------------------------------------------------------------------

class Listener:
    """
    A single TCP server endpoint that forwards requests to the shared ModBus
    device and optionally converts register values in replies.

    Multiple Listener instances can share one ModBus connection, so the
    physical device is polled only once per request cycle regardless of
    how many listeners (and clients) are configured.
    """

    def __init__(self, modbus, bind_url, conversions=None, unit_id_remapping=None):
        url = parse_url(bind_url)
        self.host = url.hostname
        self.port = 502 if url.port is None else url.port
        self.modbus = modbus
        self.unit_id_remapping = unit_id_remapping or {}
        self.converter = SunSpecConverter(conversions) if conversions else None
        self.server = None
        self.log = log.getChild(f"Listener(:{self.port})")
        if conversions:
            self.log.info("SunSpec conversions: %s", conversions)

    @property
    def address(self):
        if self.listeners:
            return self.listeners[0].address

    def _transform_request(self, request):
        uid = request[6]
        new_uid = self.unit_id_remapping.setdefault(uid, uid)
        if uid != new_uid:
            request = bytearray(request)
            request[6] = new_uid
        return request

    def _transform_reply(self, request, reply):
        # Reverse unit ID remapping
        uid = reply[6]
        inverse = {v: k for k, v in self.unit_id_remapping.items()}
        new_uid = inverse.setdefault(uid, uid)
        if uid != new_uid:
            reply = bytearray(reply)
            reply[6] = new_uid
            reply = bytes(reply)
        # SunSpec conversion
        if self.converter is not None:
            self.converter.update_sf_cache(request, reply)
            reply = self.converter.transform_reply(request, reply)
        return reply

    async def handle_client(self, reader, writer):
        async with Client(reader, writer) as client:
            while True:
                request = await client.read()
                if not request:
                    break
                transformed = self._transform_request(request)
                reply = await self.modbus.write_read(transformed)
                if not reply:
                    break
                result = await client.write(self._transform_reply(request, reply))
                if not result:
                    break

    async def start(self):
        if self.converter is not None:
            await self.converter.warmup(self.modbus.write_read)
        self.server = await asyncio.start_server(
            self.handle_client, self.host, self.port, start_serving=True
        )
        self.log.info("Ready to accept requests on %s:%d", self.host, self.port)

    async def stop(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def serve_forever(self):
        if self.server is None:
            await self.start()
        async with self.server:
            await self.server.serve_forever()


# ---------------------------------------------------------------------------
# ModBus — one connection to the physical device, shared by all Listeners
# ---------------------------------------------------------------------------

class ModBus(Connection):
    def __init__(self, config):
        modbus = config["modbus"]
        url = parse_url(modbus["url"])
        super().__init__(f"ModBus({url.hostname}:{url.port})", None, None)
        self.modbus_host = url.hostname
        self.modbus_port = url.port
        self.timeout = modbus.get("timeout", None)
        self.connection_time = modbus.get("connection_time", 0)
        self.lock = asyncio.Lock()
        self.server = None  # kept for address property compatibility

        # Build listeners
        # Support both legacy "listen" (single) and new "listeners" (multiple)
        listeners_cfg = config.get("listeners")
        if listeners_cfg is None:
            # Legacy: single "listen" key
            listen_cfg = config.get("listen", {})
            bind = listen_cfg.get("bind", ":502")
            conversions_cfg = config.get("register_conversions") or []
            conversions = [RegisterConversion.from_config(c) for c in conversions_cfg]
            unit_id_remapping = config.get("unit_id_remapping") or {}
            self.listeners = [
                Listener(
                    modbus=self,
                    bind_url=bind,
                    conversions=conversions or None,
                    unit_id_remapping=unit_id_remapping,
                )
            ]
        else:
            self.listeners = []
            for lcfg in listeners_cfg:
                bind = lcfg.get("bind", ":502")
                conversions_cfg = lcfg.get("register_conversions") or []
                conversions = [RegisterConversion.from_config(c) for c in conversions_cfg]
                unit_id_remapping = lcfg.get("unit_id_remapping") or {}
                self.listeners.append(
                    Listener(
                        modbus=self,
                        bind_url=bind,
                        conversions=conversions or None,
                        unit_id_remapping=unit_id_remapping,
                    )
                )
    @property
    def address(self):
        """Return the address of the first listener (backwards compatibility)."""
        if self.listeners and self.listeners[0].server is not None:
            return self.listeners[0].server.sockets[0].getsockname()
        return None

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

    async def write_read(self, data, attempts=2):
        async with self.lock:
            for i in range(attempts):
                try:
                    await self.connect()
                    return await asyncio.wait_for(self._write_read(data), self.timeout)
                except Exception as error:
                    self.log.error(
                        "write_read error [%s/%s]: %r", i + 1, attempts, error
                    )
                    await self.close()

    async def _write_read(self, data):
        await self._write(data)
        return await self._read()

    async def start(self):
        for listener in self.listeners:
            await listener.start()

    async def stop(self):
        for listener in self.listeners:
            await listener.stop()
        await self.close()

    async def serve_forever(self):
        # start() is called by run_bridges via start_bridges — don't call again
        coros = [listener.serve_forever() for listener in self.listeners]
        await asyncio.gather(*coros)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        await self.stop()


# ---------------------------------------------------------------------------
# Config / bootstrap (unchanged from original)
# ---------------------------------------------------------------------------

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
    parser.add_argument("-c", "--config-file", default=None, type=str)
    parser.add_argument("-b", "--bind", default=None, type=str)
    parser.add_argument("--modbus", default=None, type=str)
    parser.add_argument("--modbus-connection-time", type=float, default=0)
    parser.add_argument("--timeout", type=float, default=10)
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
