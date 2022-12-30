import argparse
import os
import collections

from serial import Serial
from umodbus.server.serial import get_server
from umodbus.server.serial.rtu import RTUServer

# Parse command line arguments
parser = argparse.ArgumentParser()
parser.add_argument("-p", "--port", default="/tmp/modbus-demo")
args = parser.parse_args()
port = args.port

# Create a tty to simulate serial line
master, slave = os.openpty()
os.symlink(os.ttyname(slave), port)

# "Trick serial to use our simulated serial line"
s = Serial(os.ttyname(master))
s.fd = master


app = get_server(RTUServer, s)
# A very simple data store which maps addresses against their values.
data_store = collections.defaultdict(int)


@app.route(slave_ids=[1], function_codes=[1, 2], addresses=list(range(0, 10)))
def read_data_store(slave_id, function_code, address):
    """ " Return value of address."""
    return data_store[address]


@app.route(slave_ids=[1], function_codes=[5, 15], addresses=list(range(0, 10)))
def write_data_store(slave_id, function_code, address, value):
    """ " Set value for address."""
    data_store[address] = value


@app.route(slave_ids=[1], function_codes=[3, 4], addresses=list(range(0, 10)))
def read_data_store(slave_id, function_code, address):
    """ " Return value of address."""
    print(f"read {slave_id=}, {function_code=}, {address=} = {data_store[address]}")
    return data_store[address]


@app.route(slave_ids=[1], function_codes=[6, 16], addresses=list(range(0, 10)))
def write_data_store(slave_id, function_code, address, value):
    """ " Set value for address."""
    print(f"write {slave_id=}, {function_code=}, {address=} with {value}")
    data_store[address] = value


if __name__ == "__main__":
    try:
        app.serve_forever()
    finally:
        app.shutdown()
        os.unlink(port)
        os.close(slave)
        s.close()
