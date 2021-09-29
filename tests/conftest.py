import asyncio

import pytest

from modbus_proxy import ModBus

# read_holding_registers(unit=1, start=1, size=4)
#      |tid | tcp   | size  |uni|cod|s .addr|nb. reg|
REQ = b"m\xf5\x00\x00\x00\x06\x01\x03\x00\x01\x00\x04"
#      |tid | tcp   | size  |uni|cod|len|   1   |   2   |   3   |   4   |
REP = b"m\xf5\x00\x00\x00\x0b\x01\x03\x08\x00\x01\x00\x02\x00\x03\x00\x04"


@pytest.fixture
async def modbus_device():
    async def cb(r, w):
        while True:
            d = await r.readexactly(6)
            n = int.from_bytes(d[4:6], "big")
            d += await r.readexactly(n)
            if d == REQ:
                w.write(REP)
                await w.drain()

    try:
        server = await asyncio.start_server(cb, host="127.0.0.1")
        server.address = server.sockets[0].getsockname()
        yield server
    finally:
        server.close()
        await server.wait_closed()


def modbus_config(modbus_device):
    return {
        "modbus": {"url": "{}:{}".format(*modbus_device.address)},
        "listen": {"bind": "127.0.0.1:0"},
    }


@pytest.fixture
async def modbus(modbus_device):
    cfg = modbus_config(modbus_device)
    modbus = ModBus(cfg)
    await modbus.start()
    modbus.device = modbus_device
    modbus.cfg = cfg
    async with modbus:
        yield modbus
