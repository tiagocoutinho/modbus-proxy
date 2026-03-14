import asyncio

import pytest_asyncio

from modbus_proxy import ModBus, tcp_to_rtu, read_rtu_frame

# read_holding_registers(unit=1, start=1, size=4)
#      |tid | tcp   | size  |uni|cod|s .addr|nb. reg|
REQ = b"m\xf5\x00\x00\x00\x06\x01\x03\x00\x01\x00\x04"
#      |tid | tcp   | size  |uni|cod|len|   1   |   2   |   3   |   4   |
REP = b"m\xf5\x00\x00\x00\x0b\x01\x03\x08\x00\x01\x00\x02\x00\x03\x00\x04"


# read_holding_registers(unit=1, start=2, size=3)
#       |tid | tcp   | size  |uni|cod|s .addr|nb. reg|
REQ2 = b"m\xf5\x00\x00\x00\x06\x01\x03\x00\x02\x00\x03"
#       |tid | tcp   | size  |uni|cod|len|   2   |   3   |   4   |
REP2 = b"m\xf5\x00\x00\x00\x09\x01\x03\x06\x00\x02\x00\x03\x00\x04"


# read_holding_registers(unit=1, start=2, size=3)
#       |tid | tcp   | size  |uni|cod|s .addr|nb. reg|
REQ3_ORIGINAL = b"m\xf5\x00\x00\x00\x06\xFF\x03\x00\x02\x00\x03"
REQ3_MODIFIED = b"m\xf5\x00\x00\x00\x06\xFE\x03\x00\x02\x00\x03"
#       |tid | tcp   | size  |uni|cod|len|   2   |   3   |   4   |
REP3_ORIGINAL = b"m\xf5\x00\x00\x00\x09\xFE\x03\x06\x00\x02\x00\x03\x00\x04"
REP3_MODIFIED = b"m\xf5\x00\x00\x00\x09\xFF\x03\x06\x00\x02\x00\x03\x00\x04"


# RTU versions of the above frames (payload only, no MBAP header, with CRC appended)
#      |uni|cod|s .addr|nb. reg|  crc  |
REQ_RTU = tcp_to_rtu(REQ)
#      |uni|cod|len|   1   |   2   |   3   |   4   |  crc  |
REP_RTU = tcp_to_rtu(REP)

#      |uni|cod|s .addr|nb. reg|  crc  |
REQ2_RTU = tcp_to_rtu(REQ2)
#      |uni|cod|len|   2   |   3   |   4   |  crc  |
REP2_RTU = tcp_to_rtu(REP2)


async def _read_rtu_request(r):
    """Read one RTU request frame from the fake device stream."""
    return await read_rtu_frame(r)


RTU_REPLIES = {REQ_RTU: REP_RTU, REQ2_RTU: REP2_RTU}


@pytest_asyncio.fixture
async def modbus_device_rtu():
    async def cb(r, w):
        while True:
            data = await _read_rtu_request(r)
            w.write(RTU_REPLIES[data])
            await w.drain()

    try:
        server = await asyncio.start_server(cb, host="127.0.0.1")
        server.address = server.sockets[0].getsockname()
        yield server
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def modbus_rtu(modbus_device_rtu):
    cfg = {
        "modbus": {
            "url": "{}:{}".format(*modbus_device_rtu.address),
            "mode": "rtuovertcp",
        },
        "listen": {"bind": "127.0.0.1:0"},
    }
    modbus = ModBus(cfg)
    await modbus.start()
    modbus.device = modbus_device_rtu
    async with modbus:
        yield modbus
    modbus_device_rtu.close()


@pytest_asyncio.fixture
async def modbus_device():
    async def cb(r, w):
        while True:
            data = await r.readexactly(6)
            size = int.from_bytes(data[4:6], "big")
            data += await r.readexactly(size)
            if data == REQ:
                reply = REP
            elif data == REQ2:
                reply = REP2
            elif data == REQ3_MODIFIED:
                reply = REP3_ORIGINAL

            w.write(reply)
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
        "unit_id_remapping": {255: 254},
    }


@pytest_asyncio.fixture
async def modbus(modbus_device):
    cfg = modbus_config(modbus_device)
    modbus = ModBus(cfg)
    await modbus.start()
    modbus.device = modbus_device
    modbus.cfg = cfg
    async with modbus:
        yield modbus
    modbus_device.close()
