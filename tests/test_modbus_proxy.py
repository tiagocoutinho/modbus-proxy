# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for `modbus_proxy` package."""

import os
import json
import asyncio
from collections import namedtuple
from urllib.parse import urlparse
from tempfile import NamedTemporaryFile

import toml
import yaml
import pytest
from unittest.mock import AsyncMock

from modbus_proxy import (
    parse_url,
    parse_args,
    load_config,
    run,
    ModBus,
    has_valid_modbus_tcp_mbap,
    TransactionIdMismatchError,
)

from .conftest import REQ, REP, REQ2, REP2, REQ3_ORIGINAL, REP3_MODIFIED


Args = namedtuple(
    "Args", "config_file bind modbus modbus_connection_time timeout"
)


CFG_YAML_TEXT = """\
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:9000
- modbus:
    url: plc2.acme.org:502
  listen:
    bind: 0:9001
"""

CFG_TOML_TEXT = """
[[devices]]

[devices.modbus]
url = "plc1.acme.org:502"
[devices.listen]
bind = "0:9000"
[[devices]]

[devices.modbus]
url = "plc2.acme.org:502"
[devices.listen]
bind = "0:9001"
"""

CFG_JSON_TEXT = """
{
  "devices": [
    {
      "modbus": {
        "url": "plc1.acme.org:502"
      },
      "listen": {
        "bind": "0:9000"
      }
    },
    {
      "modbus": {
        "url": "plc2.acme.org:502"
      },
      "listen": {
        "bind": "0:9001"
      }
    }
  ]
}
"""


class Ready(asyncio.Event):
    def set(self, data):
        self.data = data
        super().set()


@pytest.mark.parametrize(
    "url, expected",
    [
        ("tcp://host:502", urlparse("tcp://host:502")),
        ("host:502", urlparse("tcp://host:502")),
        ("tcp://:502", urlparse("tcp://0:502")),
        (":502", urlparse("tcp://0:502")),
    ],
    ids=["scheme://host:port", "host:port", "scheme://:port", ":port"],
)
def test_parse_url(url, expected):
    assert parse_url(url) == expected


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-c", "conf.yml"], Args("conf.yml", None, None, 0, 10)),
        (["--config-file", "conf.yml"], Args("conf.yml", None, None, 0, 10)),
    ],
    ids=["-c", "--config-file"],
)
def test_parse_args(args, expected):
    result = parse_args(args)
    assert result.config_file == expected.config_file
    assert result.bind == expected.bind
    assert result.modbus == expected.modbus
    assert result.modbus_connection_time == expected.modbus_connection_time
    assert result.timeout == expected.timeout


@pytest.mark.parametrize(
    "text, parser, suffix",
    [
        (CFG_YAML_TEXT, yaml.safe_load, ".yml"),
        (CFG_TOML_TEXT, toml.loads, ".toml"),
        (CFG_JSON_TEXT, json.loads, ".json"),
    ],
    ids=["yaml", "toml", "json"],
)
def test_load_config(text, parser, suffix):
    with NamedTemporaryFile("w+", suffix=suffix, delete=False) as f:
        f.write(text)
    try:
        config = load_config(f.name)
    finally:
        os.remove(f.name)
    assert parser(text) == config


async def open_connection(modbus):
    return await asyncio.open_connection(*modbus.address)


async def make_requests(modbus, requests):
    reader, writer = await open_connection(modbus)
    for request, reply in requests:
        writer.write(request)
        await writer.drain()
        assert await reader.readexactly(len(reply)) == reply
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize(
    "req, rep",
    [
        (REQ, REP),
        (REQ2, REP2),
        (REQ3_ORIGINAL, REP3_MODIFIED),
    ],
    ids=["req1", "req2", "req3"],
)
@pytest.mark.asyncio
async def test_modbus(modbus, req, rep):

    assert not modbus.opened

    await make_requests(modbus, [(req, rep)])

    assert modbus.opened

    # Don't make any request
    _, w = await open_connection(modbus)
    w.close()
    await w.wait_closed()
    await make_requests(modbus, [(req, rep)])

    # Don't wait for answer
    _, w = await open_connection(modbus)
    w.write(REQ)
    await w.drain()
    w.close()
    await w.wait_closed()
    await make_requests(modbus, [(req, rep)])


@pytest.mark.asyncio
async def test_concurrent_clients(modbus):
    task1 = asyncio.create_task(make_requests(modbus, 10 * [(REQ, REP)]))
    task2 = asyncio.create_task(make_requests(modbus, 12 * [(REQ2, REP2)]))
    await task1
    await task2


@pytest.mark.asyncio
async def test_concurrent_clients_with_misbihaved(modbus):
    task1 = asyncio.create_task(make_requests(modbus, 10 * [(REQ, REP)]))
    task2 = asyncio.create_task(make_requests(modbus, 12 * [(REQ2, REP2)]))

    async def misbihaved(n):
        for i in range(n):
            # Don't make any request
            _, writer = await open_connection(modbus)
            writer.close()
            await writer.wait_closed()
            await make_requests(modbus, [(REQ, REP)])

            # Don't wait for answer
            _, writer = await open_connection(modbus)
            writer.write(REQ2)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

    task3 = asyncio.create_task(misbihaved(10))
    await task1
    await task2
    await task3


@pytest.mark.parametrize(
    "req, rep",
    [
        (REQ, REP),
        (REQ2, REP2),
    ],
    ids=["req1", "req2"],
)
@pytest.mark.asyncio
async def test_run(modbus_device, req, rep):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        await make_requests(modbus, [(req, rep)])
    finally:
        for bridge in ready.data:
            await bridge.stop()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_device_not_connected(modbus):
    modbus.device.close()
    await modbus.device.wait_closed()

    with pytest.raises(asyncio.IncompleteReadError):
        await make_requests(modbus, [(REQ, REP)])


def make_frame(
    tid: int, unit: int = 1, fc: int = 3, data: bytes = b"\x00\x01\x00\x04"
) -> bytes:
    payload = bytes([unit, fc]) + data
    mbap = tid.to_bytes(2, "big") + b"\x00\x00" + len(payload).to_bytes(2, "big")
    return mbap + payload


def make_reply(
    tid: int,
    unit: int = 1,
    fc: int = 3,
    data: bytes = b"\x08\x00\x01\x00\x02\x00\x03\x00\x04",
) -> bytes:
    payload = bytes([unit, fc]) + data
    mbap = tid.to_bytes(2, "big") + b"\x00\x00" + len(payload).to_bytes(2, "big")
    return mbap + payload


def test_has_valid_modbus_tcp_mbap():
    # Valid frame (12 bytes, PID 0, length 6)
    valid_frame = make_frame(1)
    assert has_valid_modbus_tcp_mbap(valid_frame) is True

    # Empty or too short (< 8 bytes)
    assert has_valid_modbus_tcp_mbap(b"") is False
    assert has_valid_modbus_tcp_mbap(b"\x00\x01\x00\x00\x00\x01\x01") is False

    # Non-zero protocol ID
    bad_pid = b"\x00\x01\x00\x01\x00\x06\x01\x03\x00\x01\x00\x04"
    assert has_valid_modbus_tcp_mbap(bad_pid) is False

    # Length field mismatch
    bad_len = b"\x00\x01\x00\x00\x00\x0a\x01\x03\x00\x01\x00\x04"
    assert has_valid_modbus_tcp_mbap(bad_len) is False


@pytest.mark.asyncio
async def test_tid_mismatch_breaks_cascade():
    """
    Central Regression Test:
    Demonstrates that TID mismatch closes the desynchronized backend connection,
    rejects the stale packet for Client A, and allows Client B to establish a fresh
    backend connection and receive its matching response without cascading desync.
    """
    connection_count = 0

    async def backend_cb(reader, writer):
        nonlocal connection_count
        connection_count += 1
        try:
            while True:
                try:
                    header = await reader.readexactly(6)
                except asyncio.IncompleteReadError:
                    break
                size = int.from_bytes(header[4:6], "big")
                frame = header + await reader.readexactly(size)
                req_tid = int.from_bytes(frame[:2], "big")

                if req_tid == 2:
                    # Backend sends orphan TID=1 followed by TID=2 on connection 1
                    writer.write(make_reply(1) + make_reply(2))
                    await writer.drain()
                else:
                    writer.write(make_reply(req_tid))
                    await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(backend_cb, host="127.0.0.1", port=0)
    backend_port = server.sockets[0].getsockname()[1]

    cfg = {
        "modbus": {"url": f"127.0.0.1:{backend_port}", "timeout": 2},
        "listen": {"bind": "127.0.0.1:0"},
    }
    proxy = ModBus(cfg)
    await proxy.start()

    async def client_exchange(tid: int):
        proxy_port = proxy.server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)
        w.write(make_frame(tid))
        await w.drain()

        resp_header = await r.read(6)
        if len(resp_header) < 6:
            w.close()
            await w.wait_closed()
            return None
        resp_size = int.from_bytes(resp_header[4:6], "big")
        resp = resp_header + await r.readexactly(resp_size)
        w.close()
        await w.wait_closed()
        return int.from_bytes(resp[:2], "big")

    try:
        # Client A requests TID 2: backend returns TID 1 -> mismatch!
        # Proxy resets connection and rejects frame -> Client A gets None (closed)
        resp_a = await client_exchange(2)
        assert resp_a is None, "Client A must NOT receive mismatched TID 1 frame"

        # Client B requests TID 3: proxy establishes fresh connection 2
        resp_b = await client_exchange(3)
        assert resp_b == 3, f"Client B must receive matching TID 3, got {resp_b}"

        # Client C requests TID 4: stream remains perfectly synchronized
        resp_c = await client_exchange(4)
        assert resp_c == 4, f"Client C must receive matching TID 4, got {resp_c}"

        assert connection_count >= 2, (
            "Backend connection must have been reset upon mismatch"
        )

    finally:
        await proxy.stop()
        server.close()


@pytest.mark.asyncio
async def test_tid_mismatch_retry_recovery():
    """
    Tests User Review Note 5:
    With attempts=2, when a TID mismatch occurs on attempt 1, the proxy
    resets the connection and retries on a fresh connection. If attempt 2
    receives the matching response, the request succeeds!
    """
    connection_attempts = 0

    async def backend_cb(reader, writer):
        nonlocal connection_attempts
        connection_attempts += 1
        try:
            while True:
                try:
                    header = await reader.readexactly(6)
                except asyncio.IncompleteReadError:
                    break
                size = int.from_bytes(header[4:6], "big")
                frame = header + await reader.readexactly(size)
                req_tid = int.from_bytes(frame[:2], "big")

                if connection_attempts == 1:
                    # First connection returns mismatched TID 9
                    writer.write(make_reply(9))
                    await writer.drain()
                else:
                    # Second connection returns correct matching TID
                    writer.write(make_reply(req_tid))
                    await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(backend_cb, host="127.0.0.1", port=0)
    backend_port = server.sockets[0].getsockname()[1]

    cfg = {
        "modbus": {"url": f"127.0.0.1:{backend_port}", "timeout": 2},
        "listen": {"bind": "127.0.0.1:0"},
    }
    proxy = ModBus(cfg)
    await proxy.start()

    try:
        proxy_port = proxy.server.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", proxy_port)

        # Send request with TID 10 (attempts=2 default)
        w.write(make_frame(10))
        await w.drain()

        resp_header = await r.readexactly(6)
        resp_size = int.from_bytes(resp_header[4:6], "big")
        resp = resp_header + await r.readexactly(resp_size)

        received_tid = int.from_bytes(resp[:2], "big")
        assert received_tid == 10, (
            f"Expected matching TID 10 after retry, got {received_tid}"
        )
        assert connection_attempts == 2, "Proxy must have reconnected on attempt 2"

        w.close()
        await w.wait_closed()
    finally:
        await proxy.stop()
        server.close()


@pytest.mark.asyncio
async def test_cancellation_closes_backend():
    """
    Verifies cancellation hygiene:
    If write_read is cancelled while awaiting backend response,
    the backend connection is closed immediately so no orphan response lingers.
    """
    backend_stalled = asyncio.Event()

    async def backend_cb(reader, writer):
        try:
            await backend_stalled.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(backend_cb, host="127.0.0.1", port=0)
    backend_port = server.sockets[0].getsockname()[1]

    cfg = {
        "modbus": {"url": f"127.0.0.1:{backend_port}", "timeout": 5},
        "listen": {"bind": "127.0.0.1:0"},
    }
    proxy = ModBus(cfg)
    await proxy.start()

    try:
        task = asyncio.create_task(proxy.write_read(make_frame(1)))
        await asyncio.sleep(0.05)

        assert proxy.opened, "Backend connection should be open"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not proxy.opened, "Backend connection must be closed upon cancellation"
    finally:
        backend_stalled.set()
        await proxy.stop()
        server.close()


@pytest.mark.asyncio
async def test_non_modbus_tcp_passthrough(modbus):
    """
    Non-MBAP frames bypass TID validation.
    """
    short_frame = b"\x01\x03\x00\x00"
    assert has_valid_modbus_tcp_mbap(short_frame) is False


@pytest.mark.asyncio
async def test_write_read_raises_transaction_id_mismatch():
    """
    Direct unit test ensuring _write_read raises TransactionIdMismatchError
    and calls self.close() when an MBAP TID mismatch is detected.
    """
    cfg = {"modbus": {"url": "localhost:502"}, "listen": {"bind": "0:502"}}
    m = ModBus(cfg)
    m._write = AsyncMock()
    m._read = AsyncMock(return_value=make_reply(99))
    m.close = AsyncMock()

    with pytest.raises(TransactionIdMismatchError):
        await m._write_read(make_frame(1))

    assert m.close.called
