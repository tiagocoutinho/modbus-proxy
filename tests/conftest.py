import collections
import os
import socketserver
import threading

import pytest
import pytest_asyncio
import serial
from umodbus.server import tcp

from umodbus.server.serial import get_server as get_serial_server
from umodbus.server.serial import rtu

from modbus_proxy import Bridge, RTUProtocol


def create_rtu_server():
    # Create a tty to simulate serial line
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    ser = serial.Serial(os.ttyname(master_fd))
    ser.fd = master_fd
    ser.is_open = True

    server = get_serial_server(rtu.RTUServer, ser)
    server.server_address = "serial://" + os.ttyname(slave_fd)
    server.store = collections.defaultdict(int)

    addresses = list(range(1000))

    @server.route(slave_ids=[1], function_codes=[1, 2, 3, 4], addresses=addresses)
    def read_data_store(slave_id, function_code, address):
        """Return value of address."""
        return server.store[address]

    @server.route(slave_ids=[1], function_codes=[5, 6, 15, 16], addresses=addresses)
    def write_data_store(slave_id, function_code, address, value):
        """Set value for address."""
        server.store[address] = value

    return server


@pytest.fixture  # (scope="session")
def modbus_rtu_device():
    server = create_rtu_server()
    server.task = threading.Thread(target=server.serve_forever)
    server.task.start()
    yield server
    server.shutdown()
    server.task.join()


@pytest_asyncio.fixture
async def modbus_rtu(modbus_rtu_device):
    cfg = {
        "modbus": {"url": modbus_rtu_device.server_address},
        "listen": {"bind": "127.0.0.1:0"},
    }
    modbus = Bridge(cfg)
    await modbus.start()
    modbus.device = modbus_rtu_device
    modbus.cfg = cfg
    async with modbus:
        yield modbus


def create_tcp_server(host="127.0.0.1", port=0):
    server = tcp.get_server(socketserver.TCPServer, (host, port), tcp.RequestHandler)
    server.allow_reuse_address = True
    server.store = collections.defaultdict(int)

    addresses = list(range(1000))

    @server.route(slave_ids=[1], function_codes=[1, 2, 3, 4], addresses=addresses)
    def read_data_store(slave_id, function_code, address):
        """Return value of address."""
        return server.store[address]

    @server.route(slave_ids=[1], function_codes=[5, 6, 15, 16], addresses=addresses)
    def write_data_store(slave_id, function_code, address, value):
        """Set value for address."""
        server.store[address] = value

    return server


@pytest.fixture  # (scope="session")
def modbus_tcp_device():
    with create_tcp_server() as server:
        server.task = threading.Thread(target=server.serve_forever)
        server.task.start()
        yield server
    server.shutdown()
    server.task.join()


@pytest_asyncio.fixture
async def modbus_tcp(modbus_tcp_device):
    cfg = {
        "modbus": {"url": "{}:{}".format(*modbus_tcp_device.server_address)},
        "listen": {"bind": "127.0.0.1:0"},
    }
    modbus = Bridge(cfg)
    await modbus.start()
    modbus.device = modbus_tcp_device
    modbus.cfg = cfg
    async with modbus:
        yield modbus
