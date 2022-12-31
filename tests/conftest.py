import asyncio
import os
import struct

import pytest_asyncio
import serialio

from modbus_proxy import Bridge, RTUProtocol

#           |cod|s .addr|nb. reg|
REQ_PDU = b"\x03\x00\x01\x00\x04"
#           |cod|len|   1   |   2   |   3   |   4   |
REP_PDU = b"\x03\x08\x00\x01\x00\x02\x00\x03\x00\x04"

# read_holding_registers(unit=1, start=1, size=4)
#          |tid | tcp   | size  |uni|
REQ_TCP = b"m\xf5\x00\x00\x00\x06\x01" + REQ_PDU
#          |tid | tcp   | size  |uni|
REP_TCP = b"m\xf5\x00\x00\x00\x0b\x01" + REP_PDU
#          |addr|              | CRC    |
REQ_RTU = b"\x01" + REQ_PDU + b"\x15\xc9"
#          |addr|              | CRC |
REP_RTU = b"\x01" + REP_PDU + b"\r\x14"

# read_holding_registers(unit=1, start=2, size=3)
#           |cod|s .addr|nb. reg|
REQ2_PDU = b"\x03\x00\x02\x00\x03"
#           |cod|len|   2   |   3   |   4   |
REP2_PDU = b"\x03\x06\x00\x02\x00\x03\x00\x04"
#           |tid | tcp   | size  |uni|
REQ2_TCP = b"m\xf5\x00\x00\x00\x06\x01" + REQ2_PDU
#           |tid | tcp   | size  |uni|
REP2_TCP = b"m\xf5\x00\x00\x00\x09\x01" + REP2_PDU
#          |addr|              | CRC    |
REQ2_RTU = b"\x01" + REQ2_PDU + b"\x15\xc9"
#          |addr|              | CRC |
REP2_RTU = b"\x01" + REP2_PDU + b"\xa9v"


@pytest_asyncio.fixture
async def modbus_tcp_device():
    async def cb(r, w):
        while True:
            data = await r.readexactly(6)
            size = int.from_bytes(data[4:6], "big")
            data += await r.readexactly(size)
            if data == REQ_TCP:
                reply = REP_TCP
            elif data == REQ2_TCP:
                reply = REP2_TCP
            else:
                raise ValueError("unexpected test packet")
            w.write(reply)
            await w.drain()

    try:
        server = await asyncio.start_server(cb, host="127.0.0.1")
        server.address = server.sockets[0].getsockname()
        yield server
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def modbus_rtu_device():
    # Create a tty to simulate serial line
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    server = serialio.serial_for_url("serial:///fake")
    server.fd = master_fd
    server.is_open = True
    server.address = "serial://" + os.ttyname(slave_fd)

    async def read_request():
        payload = await server.read(7)
        _, func, _, _, byte_count = struct.unpack(">BBHHB", payload)
        if func in RTUProtocol.REQ_STATIC:
            byte_count = 1  # second byte of CRC
        elif func in RTUProtocol.REQ_DYNAMIC:
            byte_count += 2  # CRC
        else:
            raise ValueError("wrong packet")
        # CRC is 2 bytes long
        payload += await server.read(byte_count)
        return payload

    async def run():
        while True:
            adu = await read_request()
            if adu == REQ_RTU:
                reply = REP_RTU
            elif adu == REQ2_RTU:
                reply = REP2_RTU
            else:
                raise ValueError("unexpected test packet")
            await server.write(reply)

    server.task = asyncio.create_task(run())
    try:
        yield server
    finally:
        server.task.cancel()
        try:
            await server.task
        except asyncio.CancelledError:
            pass
        os.close(master_fd)
        os.close(slave_fd)


def modbus_tcp_config(modbus_tcp_device):
    return {
        "modbus": {"url": "{}:{}".format(*modbus_tcp_device.address)},
        "listen": {"bind": "127.0.0.1:0"},
    }


def modbus_rtu_config(modbus_rtu_device):
    return {
        "modbus": {"url": modbus_rtu_device.address},
        "listen": {"bind": "127.0.0.1:0"},
    }


@pytest_asyncio.fixture
async def modbus_tcp(modbus_tcp_device):
    cfg = modbus_tcp_config(modbus_tcp_device)
    modbus = Bridge(cfg)
    await modbus.start()
    modbus.device = modbus_tcp_device
    modbus.cfg = cfg
    async with modbus:
        yield modbus


@pytest_asyncio.fixture
async def modbus_rtu(modbus_rtu_device):
    cfg = modbus_rtu_config(modbus_rtu_device)
    modbus = Bridge(cfg)
    await modbus.start()
    modbus.device = modbus_rtu_device
    modbus.cfg = cfg
    async with modbus:
        yield modbus
