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

from modbus_proxy import parse_url, parse_args, transport_protocol_for_url, load_config, run

from .conftest import REQ_TCP, REP_TCP, REQ2_TCP, REP2_TCP
from .conftest import REQ_RTU, REP_RTU, REQ2_RTU, REP2_RTU

Args = namedtuple(
    "Args", "config_file bind modbus modbus_connection_time timeout log_config_file"
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
    "url, expected",
    [
        ("host:502", ("tcp", "tcp")),
        ("tcp://host:502", ("tcp", "tcp")),
        ("tcp://:502", ("tcp", "tcp")),
        (":502", ("tcp", "tcp")),
        ("tcp+rtu://host:502", ("tcp", "rtu")),
        ("tcp+rtu://:502", ("tcp", "rtu")),
        ("serial:///dev/ttyS0", ("serial", "rtu")),
        ("rfc2217://host:502", ("rfc2217", "rtu")),
        ("serial+tcp:///dev/ttyS0", ("serial", "tcp")),
        ("serial+tcp+rtu:///dev/ttyS0", ("serial+tcp", "rtu")),
    ]
)
def test_transport_protocol_for_url(url, expected):
    assert transport_protocol_for_url(url) == expected


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-c", "conf.yml"], Args("conf.yml", None, None, 0, 10, None)),
        (["--config-file", "conf.yml"], Args("conf.yml", None, None, 0, 10, None)),
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
    assert result.log_config_file == expected.log_config_file


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


async def open_connection(modbus_tcp):
    return await asyncio.open_connection(*modbus_tcp.address)


async def make_requests(modbus_tcp, requests):
    reader, writer = await open_connection(modbus_tcp)
    for request, reply in requests:
        writer.write(request)
        await writer.drain()
        assert await reader.readexactly(len(reply)) == reply
    writer.close()
    await writer.wait_closed()


async def make_rtu_requests(modbus_rtu, requests):
    serial = modbus_rtu.device
    for request, reply in requests:
        await serial.write(request)
        assert await serial.read(len(reply)) == reply


@pytest.mark.parametrize(
    "req, rep",
    [
        (REQ_TCP, REP_TCP),
        (REQ2_TCP, REP2_TCP),
    ],
    ids=["req1", "req2"],
)
@pytest.mark.asyncio
async def test_modbus(modbus_tcp, req, rep):

    assert not modbus_tcp.is_open

    await make_requests(modbus_tcp, [(req, rep)])

    assert modbus_tcp.is_open

    # Don't make any request
    _, w = await open_connection(modbus_tcp)
    w.close()
    await w.wait_closed()
    await make_requests(modbus_tcp, [(req, rep)])

    # Don't wait for answer
    _, w = await open_connection(modbus_tcp)
    w.write(REQ_TCP)
    await w.drain()
    w.close()
    await w.wait_closed()
    await make_requests(modbus_tcp, [(req, rep)])


@pytest.mark.parametrize(
    "req, rep",
    [
        (REQ_RTU, REP_RTU),
        (REQ2_RTU, REP2_RTU),
    ],
    ids=["req1", "req2"],
)
@pytest.mark.asyncio
async def test_modbus_rtu(modbus_rtu, req, rep):

    assert not modbus_rtu.is_open

    await make_requests(modbus_rtu, [(req, rep)])

    assert modbus_rtu.is_open

    # Don't make any request
    _, w = await open_connection(modbus_rtu)
    w.close()
    await w.wait_closed()
    await make_requests(modbus_rtu, [(req, rep)])

    # Don't wait for answer
    _, w = await open_connection(modbus_rtu)
    w.write(REQ_RTU)
    await w.drain()
    w.close()
    await w.wait_closed()
    await make_requests(modbus_rtu, [(req, rep)])


@pytest.mark.asyncio
async def test_concurrent_clients(modbus_tcp):
    task1 = asyncio.create_task(make_requests(modbus_tcp, 10 * [(REQ_TCP, REP_TCP)]))
    task2 = asyncio.create_task(make_requests(modbus_tcp, 12 * [(REQ2_TCP, REP2_TCP)]))
    await task1
    await task2


@pytest.mark.asyncio
async def test_concurrent_clients_with_misbihaved(modbus_tcp):
    task1 = asyncio.create_task(make_requests(modbus_tcp, 10 * [(REQ_TCP, REP_TCP)]))
    task2 = asyncio.create_task(make_requests(modbus_tcp, 12 * [(REQ2_TCP, REP2_TCP)]))

    async def misbihaved(n):
        for i in range(n):
            # Don't make any request
            _, writer = await open_connection(modbus_tcp)
            writer.close()
            await writer.wait_closed()
            await make_requests(modbus_tcp, [(REQ_TCP, REP_TCP)])

            # Don't wait for answer
            _, writer = await open_connection(modbus_tcp)
            writer.write(REQ2_TCP)
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
        (REQ_TCP, REP_TCP),
        (REQ2_TCP, REP2_TCP),
    ],
    ids=["req1", "req2"],
)
@pytest.mark.asyncio
async def test_run(modbus_tcp_device, req, rep):
    addr = "{}:{}".format(*modbus_tcp_device.address)
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
async def test_device_not_connected(modbus_tcp):
    modbus_tcp.device.close()
    await modbus_tcp.device.wait_closed()

    with pytest.raises(asyncio.IncompleteReadError):
        await make_requests(modbus_tcp, [(REQ_TCP, REP_TCP)])
