# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for `modbus_proxy` package."""

import asyncio
import contextlib
import json
import os
import sys
from collections import namedtuple
from urllib.parse import urlparse
from tempfile import NamedTemporaryFile

import toml
import yaml
import pytest

from umodbus.client import tcp
from umodbus.client.serial import rtu

from modbus_proxy import (
    parse_url,
    parse_args,
    transport_protocol_for_url,
    load_config,
    run,
)

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


@contextlib.asynccontextmanager
async def open_connection(modbus_tcp):
    reader, writer = await asyncio.open_connection(*modbus_tcp.server_address)
    try:
        yield reader, writer
    finally:
        writer.close()
        await writer.wait_closed()


async def send_tcp_message(adu, reader, writer):
    writer.write(adu)
    await writer.drain()
    exception_adu_size = 9
    response_error_adu = await reader.readexactly(exception_adu_size)
    tcp.raise_for_exception_adu(response_error_adu)

    expected_response_size = (
        tcp.expected_response_pdu_size_from_request_pdu(adu[7:]) + 7
    )
    response_remainder = await reader.readexactly(
        expected_response_size - exception_adu_size
    )

    return tcp.parse_response_adu(response_error_adu + response_remainder, adu)


async def send_rtu_message(adu, reader, writer):
    writer.write(adu)
    await writer.drain()
    # Check exception ADU (which is shorter than all other responses) first.
    exception_adu_size = 5
    response_error_adu = await reader.readexactly(exception_adu_size)
    rtu.raise_for_exception_adu(response_error_adu)

    expected_response_size = (
        rtu.expected_response_pdu_size_from_request_pdu(adu[1:-2]) + 3
    )
    response_remainder = await reader.readexactly(
        expected_response_size - exception_adu_size
    )

    return rtu.parse_response_adu(response_error_adu + response_remainder, adu)


async def make_rtu_request(modbus, message, expected):
    async with open_connection(modbus) as (reader, writer):
        response = await make_rtu_request(message, reader, writer)
        assert response == expected


async def make_request(modbus, message, expected):
   async with open_connection(modbus) as (reader, writer):
        response = await send_tcp_message(message, reader, writer)
        assert response == expected


async def make_multiple_requests(modbus, messages, expected):
    async with open_connection(modbus) as (reader, writer):
        for message, expect in zip(messages, expected):
            response = await send_tcp_message(message, reader, writer)
            assert response == expect


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
    ],
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


@pytest.mark.parametrize(
    "message, expected, store",
    [
        (
            rtu.read_holding_registers(1, 1, 4),
            [10, 20, 30, 40],
            {1: 10, 2: 20, 3: 30, 4: 40},
        ),
        (
            rtu.read_coils(1, 10, 5),
            [1, 0, 1, 1, 0],
            {10: 1, 11: 0, 12: 1, 13: 1, 14: 0},
        ),
        (rtu.read_discrete_inputs(1, 4, 3), [1, 0, 1], {4: 1, 5: 0, 6: 1}),
    ],
)
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="not a linux OS")
async def test_modbus_rtu_read(modbus_rtu, message, expected, store):
    assert not modbus_rtu.is_open

    modbus_rtu.device.store = store

    async with open_connection(modbus_rtu) as (first_reader, first_writer):
        assert await send_rtu_message(message, first_reader, first_writer) == expected

        assert modbus_rtu.is_open

        # Don't make any request
        async with open_connection(modbus_rtu):
            pass

        assert await send_rtu_message(message, first_reader, first_writer) == expected

        # Don't wait for answer
        async with open_connection(modbus_rtu) as (_, writer):
            writer.write(message)
            await writer.drain()

        assert await send_rtu_message(message, first_reader, first_writer) == expected


@pytest.mark.parametrize(
    "message, expected, expected_store",
    [
        (rtu.write_single_coil(1, 3, 1), 1, {3: 1}),
        (rtu.write_single_coil(1, 2, 0), 0, {2: 0}),
    ],
)
@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="not a linux OS")
async def test_modbus_rtu_write(modbus_rtu, message, expected, expected_store):
    assert not modbus_rtu.is_open

    modbus_rtu.device.store = {}

    async with open_connection(modbus_rtu) as (first_reader, first_writer):
        assert await send_rtu_message(message, first_reader, first_writer) == expected
        assert modbus_rtu.device.store == expected_store

        assert modbus_rtu.is_open

        # Don't make any request
        modbus_rtu.device.store = {}

        async with open_connection(modbus_rtu):
            pass

        assert await send_rtu_message(message, first_reader, first_writer) == expected
        assert modbus_rtu.device.store == expected_store

        # Don't wait for answer
        async with open_connection(modbus_rtu) as (_, writer):
            writer.write(message)
            await writer.drain()

        modbus_rtu.device.store = {}
        assert await send_rtu_message(message, first_reader, first_writer) == expected
        assert modbus_rtu.device.store == expected_store


@pytest.mark.parametrize(
    "message, expected, store",
    [
        (
            tcp.read_holding_registers(1, 1, 4),
            [10, 20, 30, 40],
            {1: 10, 2: 20, 3: 30, 4: 40},
        ),
        (
            tcp.read_coils(1, 10, 5),
            [1, 0, 1, 1, 0],
            {10: 1, 11: 0, 12: 1, 13: 1, 14: 0},
        ),
        (tcp.read_discrete_inputs(1, 4, 3), [1, 0, 1], {4: 1, 5: 0, 6: 1}),
    ],
)
@pytest.mark.asyncio
async def test_modbus_tcp_read(modbus_tcp, message, expected, store):
    assert not modbus_tcp.is_open

    modbus_tcp.device.store = store

    async with open_connection(modbus_tcp) as (first_reader, first_writer):
        assert await send_tcp_message(message, first_reader, first_writer) == expected

        assert modbus_tcp.is_open

        # Don't make any request
        async with open_connection(modbus_tcp):
            pass

        assert await send_tcp_message(message, first_reader, first_writer) == expected

        # Don't wait for answer
        async with open_connection(modbus_tcp) as (_, writer):
            writer.write(message)
            await writer.drain()

        assert await send_tcp_message(message, first_reader, first_writer) == expected


@pytest.mark.parametrize(
    "message, expected, expected_store",
    [
        (tcp.write_single_coil(1, 3, 1), 1, {3: 1}),
        (tcp.write_single_coil(1, 2, 0), 0, {2: 0}),
    ],
)
@pytest.mark.asyncio
async def test_modbus_tcp_write(modbus_tcp, message, expected, expected_store):
    assert not modbus_tcp.is_open

    modbus_tcp.device.store = {}

    async with open_connection(modbus_tcp) as (first_reader, first_writer):
        assert await send_tcp_message(message, first_reader, first_writer) == expected
        assert modbus_tcp.device.store == expected_store

        assert modbus_tcp.is_open

        # Don't make any request
        modbus_tcp.device.store = {}

        async with open_connection(modbus_tcp):
            pass

        assert await send_tcp_message(message, first_reader, first_writer) == expected
        assert modbus_tcp.device.store == expected_store

        # Don't wait for answer
        modbus_tcp.device.store = {}
        async with open_connection(modbus_tcp) as (_, writer):
            writer.write(message)
            await writer.drain()

        modbus_tcp.device.store = {}
        assert await send_tcp_message(message, first_reader, first_writer) == expected
        assert modbus_tcp.device.store == expected_store


@pytest.mark.asyncio
async def test_concurrent_clients(modbus_tcp):
    modbus_tcp.device.store = [None] + list(range(1, 10))
    message1 = tcp.read_holding_registers(1, 1, 4)
    message2 = tcp.read_holding_registers(1, 5, 5)
    task1 = asyncio.create_task(make_request(modbus_tcp, message1, [1, 2, 3, 4]))
    task2 = asyncio.create_task(make_request(modbus_tcp, message2, [5, 6, 7, 8, 9]))
    await task1
    await task2


@pytest.mark.asyncio
async def test_concurrent_clients_with_misbihaved(modbus_tcp):
    modbus_tcp.device.store = [None] + list(range(1, 10))
    m1 = tcp.read_holding_registers(1, 1, 4)
    m2 = tcp.read_holding_registers(1, 5, 5)
    e1 = [1, 2, 3, 4]
    e2 = [5, 6, 7, 8, 9]

    messages = [m1, m2, m1, m2, m1, m2, m1, m2, m1, m2]
    expected = [e1, e2, e1, e2, e1, e2, e1, e2, e1, e2]
    task1 = asyncio.create_task(make_multiple_requests(modbus_tcp, messages, expected))

    messages = [m1, m2, m2, m2, m1, m1, m1, m2, m1, m1, m2, m2]
    expected = [e1, e2, e2, e2, e1, e1, e1, e2, e1, e1, e2, e2]
    task2 = asyncio.create_task(make_multiple_requests(modbus_tcp, messages, expected))

    async def misbihaved(n):
        for i in range(n):
            # Don't make any request
            async with open_connection(modbus_tcp):
                pass
            await make_request(modbus_tcp, m1, e1)

            # Don't wait for answer
            async with open_connection(modbus_tcp) as (_, writer):
                writer.write(m2)
                await writer.drain()

    task3 = asyncio.create_task(misbihaved(10))
    await task1
    await task2
    await task3


@pytest.mark.parametrize(
    "message, expected, store",
    [
        (tcp.read_holding_registers(1, 1, 2), [10, 20], {1: 10, 2: 20}),
        (tcp.read_coils(1, 10, 3), [1, 0, 1], {10: 1, 11: 0, 12: 1}),
    ],
    ids=["read_holding_registers", "read_coils"],
)
@pytest.mark.asyncio
async def test_run(modbus_tcp_device, message, expected, store):
    modbus_tcp_device.store = store
    addr = "{}:{}".format(*modbus_tcp_device.server_address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        await make_request(modbus, message, expected)
    finally:
        for bridge in ready.data:
            await bridge.stop()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_device_not_connected(modbus_tcp):
    modbus_tcp.device.server_close()

    message = tcp.read_holding_registers(1, 1, 4)
    with pytest.raises(asyncio.IncompleteReadError):
        await make_request(modbus_tcp, message, None)
