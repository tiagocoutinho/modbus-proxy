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

from unittest.mock import AsyncMock, patch

from modbus_proxy import parse_url, parse_args, load_config, run, ModBus, create_config

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


def test_config_attempts_and_retry_count(caplog):
    # Default attempts
    cfg = {"modbus": {"url": "localhost:502"}, "listen": {"bind": "0:502"}}
    m = ModBus(cfg)
    assert m.attempts == 2
    assert m.reconnect_delay == 0.0

    # Explicit attempts: 1
    cfg = {
        "modbus": {"url": "localhost:502", "attempts": 1},
        "listen": {"bind": "0:502"},
    }
    m = ModBus(cfg)
    assert m.attempts == 1

    # Explicit attempts: 3 and reconnect_delay: 0.5
    cfg = {
        "modbus": {"url": "localhost:502", "attempts": 3, "reconnect_delay": 0.5},
        "listen": {"bind": "0:502"},
    }
    m = ModBus(cfg)
    assert m.attempts == 3
    assert m.reconnect_delay == 0.5

    # Deprecated retry_count mapping
    cfg = {
        "modbus": {"url": "localhost:502", "retry_count": 2},
        "listen": {"bind": "0:502"},
    }
    with caplog.at_level("WARNING"):
        m = ModBus(cfg)
    assert m.attempts == 3
    assert "retry_count' is deprecated" in caplog.text


@pytest.mark.parametrize(
    "invalid_cfg",
    [
        {"attempts": 0},
        {"attempts": -2},
        {"retry_count": -1},
        {"reconnect_delay": -0.1},
    ],
)
def test_config_attempts_invalid(invalid_cfg):
    cfg = {
        "modbus": {"url": "localhost:502", **invalid_cfg},
        "listen": {"bind": "0:502"},
    }
    with pytest.raises(ValueError):
        ModBus(cfg)


def test_parse_args_attempts():
    args = parse_args(
        [
            "--modbus",
            "localhost:502",
            "--attempts",
            "1",
            "--reconnect-delay",
            "0.5",
        ]
    )
    assert args.attempts == 1
    assert args.reconnect_delay == 0.5

    cfg = create_config(args)
    assert cfg["devices"][0]["modbus"]["attempts"] == 1
    assert cfg["devices"][0]["modbus"]["reconnect_delay"] == 0.5


def test_parse_args_invalid():
    with pytest.raises(SystemExit):
        parse_args(["--modbus", "localhost:502", "--attempts", "0"])
    with pytest.raises(SystemExit):
        parse_args(["--modbus", "localhost:502", "--reconnect-delay", "-1"])


@pytest.mark.parametrize("attempts", [1, 2, 3])
@pytest.mark.asyncio
async def test_write_read_attempts_count(attempts):
    cfg = {
        "modbus": {"url": "localhost:502", "attempts": attempts, "timeout": 0.05},
        "listen": {"bind": "0:502"},
    }
    m = ModBus(cfg)
    calls = 0

    async def mock_connect():
        pass

    async def mock_close():
        pass

    async def mock_write_read(data):
        nonlocal calls
        calls += 1
        raise asyncio.TimeoutError("Backend timeout")

    m.connect = mock_connect
    m.close = mock_close
    m._write_read = mock_write_read

    result = await m.write_read(b"dummy")
    assert result is None
    assert calls == attempts
    assert not m.lock.locked()


@pytest.mark.asyncio
async def test_write_read_reconnect_delay():
    cfg = {
        "modbus": {
            "url": "localhost:502",
            "attempts": 2,
            "timeout": 0.05,
            "reconnect_delay": 0.02,
        },
        "listen": {"bind": "0:502"},
    }
    m = ModBus(cfg)
    sleep_calls = []

    async def mock_connect():
        pass

    async def mock_close():
        pass

    async def mock_write_read(data):
        raise OSError("Connection failed")

    async def mock_sleep(seconds):
        sleep_calls.append(seconds)

    m.connect = mock_connect
    m.close = mock_close
    m._write_read = mock_write_read

    with patch("asyncio.sleep", side_effect=mock_sleep):
        result = await m.write_read(b"dummy")

    assert result is None
    assert sleep_calls == [0.02]


@pytest.mark.asyncio
async def test_lock_release_on_failure():
    cfg = {
        "modbus": {"url": "localhost:502", "attempts": 1, "timeout": 0.05},
        "listen": {"bind": "0:502"},
    }
    m = ModBus(cfg)

    async def failing_write_read(data):
        raise asyncio.TimeoutError("Timeout")

    m.connect = AsyncMock()
    m.close = AsyncMock()
    m._write_read = failing_write_read

    await m.write_read(b"dummy")
    assert not m.lock.locked(), (
        "Lock must be released immediately after single attempt failure"
    )

    async with m.lock:
        assert m.lock.locked()
