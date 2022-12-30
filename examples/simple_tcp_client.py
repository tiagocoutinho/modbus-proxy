import time
from argparse import ArgumentParser
from socket import create_connection

from umodbus.client import tcp

parser = ArgumentParser()
parser.add_argument("-a", "--address", default="127.0.0.1:5021")
args = parser.parse_args()
host, port = args.address.rsplit(":", 1)
port = int(port)

values = [1, 2, 3, 4]


with create_connection((host, port)) as sock:
    message = tcp.write_multiple_registers(1, 1, values)
    response = tcp.send_message(message, sock)
    assert response == 4

    message = tcp.read_holding_registers(1, 1, quantity=len(values))
    response = tcp.send_message(message, sock)
    assert response == values
    print("holding registers:", response)
