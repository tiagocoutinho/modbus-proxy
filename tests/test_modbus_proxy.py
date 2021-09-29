# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for `modbus_proxy` package."""

import asyncio
from collections import namedtuple
from urllib.parse import urlparse

import pytest

from modbus_proxy import parse_url, parse_args, run

from .conftest import REQ, REP


Args = namedtuple(
    "Args", "config_file bind modbus modbus_connection_time timeout log_config_file"
)


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
)
def test_parse_url(url, expected):
    assert parse_url(url) == expected


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-c", "conf.yml"], Args("conf.yml", None, None, 0, 10, None)),
        (["--config-file", "conf.yml"], Args("conf.yml", None, None, 0, 10, None)),
    ],
)
def test_parse_args(args, expected):
    result = parse_args(args)
    assert result.config_file == expected.config_file
    assert result.bind == expected.bind
    assert result.modbus == expected.modbus
    assert result.modbus_connection_time == expected.modbus_connection_time
    assert result.timeout == expected.timeout
    assert result.log_config_file == expected.log_config_file


@pytest.mark.asyncio
async def test_modbus(modbus):

    assert not modbus.opened

    r, w = await asyncio.open_connection(*modbus.address)

    assert not modbus.opened

    w.write(REQ)
    await w.drain()

    assert await r.readexactly(len(REP)) == REP

    assert modbus.opened

    w.close()
    await w.wait_closed()


@pytest.mark.asyncio
async def test_run(modbus_device):
    addr = "{}:{}".format(*modbus_device.address)
    args = ["--modbus", addr, "--bind", "127.0.0.1:0"]
    ready = Ready()
    task = asyncio.create_task(run(args, ready))
    try:
        await ready.wait()
        modbus = ready.data[0]
        r, w = await asyncio.open_connection(*modbus.address)
        w.write(REQ)
        await w.drain()
        assert await r.readexactly(len(REP)) == REP
        w.close()
        await w.wait_closed()
    finally:
        for bridge in ready.data:
            await bridge.close()
        try:
            await task
        except asyncio.CancelledError:
            pass
