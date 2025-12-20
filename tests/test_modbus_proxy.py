# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for `modbus_proxy` package."""

import os
import time
import json
import asyncio
from collections import namedtuple
from urllib.parse import urlparse
from tempfile import NamedTemporaryFile

import toml
import yaml
import pytest

from modbus_proxy import parse_url, parse_args, load_config, run

from .conftest import REQ, REP, REQ2, REP2, REQ3_ORIGINAL, REP3_MODIFIED


Args = namedtuple(
    "Args", "config_file bind modbus modbus_connection_time timeout modbus_idle_time modbus_connection_ttl modbus_reconnect_delay modbus_request_delay"
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
        (["-c", "conf.yml"], Args("conf.yml", None, None, 0, 10, 0, 0, 0, 0)),
        (["--config-file", "conf.yml"], Args("conf.yml", None, None, 0, 10, 0, 0, 0, 0)),
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
    assert result.modbus_idle_time == expected.modbus_idle_time
    assert result.modbus_connection_ttl == expected.modbus_connection_ttl
    assert result.modbus_reconnect_delay == expected.modbus_reconnect_delay
    assert result.modbus_request_delay == expected.modbus_request_delay


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


@pytest.mark.parametrize(
    "req, rep, idle",
    [
        (REQ, REP, 0),
        (REQ, REP, 2),
        (REQ, REP, 5),
    ],
    ids=["disabled", "2s", "5s"],
)
@pytest.mark.asyncio
async def test_idle_time(modbus_device, req, rep, idle):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0", "--modbus-idle-time", f"{idle}"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        assert modbus.idle_time == idle
        await make_requests(modbus, [(req, rep)])
        assert modbus.opened

        # After the idle_time (plus 1 sec for hard-coded delay
        # and 1 second for sleep jitter) the connection is closed
        # if idle_time > 0.
        await asyncio.sleep(idle + 2)
        assert (idle > 0 and not modbus.opened) or modbus.opened
    finally:
        for bridge in ready.data:
            await bridge.stop()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize(
    "req, rep, ttl",
    [
        (REQ, REP, 0),
        (REQ, REP, 10),
    ],
    ids=["disabled", "10s"],
)
@pytest.mark.asyncio
async def test_connection_ttl(modbus_device, req, rep, ttl):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0", "--modbus-connection-ttl", f"{ttl}"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        assert modbus.connection_ttl == ttl
        await make_requests(modbus, [(req, rep)])

        if ttl > 0:
            # Wait less then connection ttl, the connection is still open.
            await asyncio.sleep(ttl - 1)
            assert modbus.opened
            await make_requests(modbus, [(req, rep)])

        # Wait again to pass the connection ttl, if enabled the connection
        # will be closed.
        await asyncio.sleep(3)
        assert (ttl > 0 and not modbus.opened) or modbus.opened
    finally:
        for bridge in ready.data:
            await bridge.stop()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize(
    "req, rep, delay",
    [
        (REQ3_ORIGINAL, REP3_MODIFIED, 0),
        (REQ3_ORIGINAL, REP3_MODIFIED, 3),
    ],
    ids=["0s", "3s"],
)
@pytest.mark.asyncio
async def test_reconnect_delay(modbus_device, req, rep, delay):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0", "--modbus-reconnect-delay", f"{delay}"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        assert modbus.reconnect_delay == delay
        t1 = time.time()
        try:
            await make_requests(modbus, [(req, rep)])
        except:
            pass

        # The proxy execute 2 requests in case of error, so the total
        # time should be greater then 2 times the delay.
        assert (time.time() - t1) > 2 * delay
    finally:
        for bridge in ready.data:
            await bridge.stop()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.parametrize(
    "req, rep, delay",
    [
        (REQ2, REP2, 0),
        (REQ2, REP2, 0.5),
        (REQ2, REP2, 2),
    ],
    ids=["0s", "0.5s", "2s"],
)
@pytest.mark.asyncio
async def test_request_delay(modbus_device, req, rep, delay):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0", "--modbus-request-delay", f"{delay}"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        assert modbus.request_delay_ns == delay * 1e9
        t1 = time.time_ns()
        await make_requests(modbus, [(req, rep)])
        await make_requests(modbus, [(req, rep)])
        t2 = time.time_ns()

        # two sequential requests should last at least modbus.request_delay_ns
        assert (t2 - t1) > modbus.request_delay_ns
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
