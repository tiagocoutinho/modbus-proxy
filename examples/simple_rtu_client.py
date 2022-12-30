import argparse
import socket

from umodbus.client.serial import rtu


parser = argparse.ArgumentParser()
parser.add_argument("-a", "--address", default="127.0.0.1:5022")
args = parser.parse_args()
host, port = args.address.rsplit(":", 1)
port = int(port)

values = [1, 2, 3, 4]

with socket.create_connection((host, port)) as sock:
    sockf = sock.makefile("rwb")

    message = rtu.write_multiple_registers(1, 1, values)
    response = rtu.send_message(message, sockf)
    assert response == 4

    message = rtu.read_holding_registers(1, 1, quantity=len(values))
    response = rtu.send_message(message, sockf)
    assert response == values
    print("holding registers:", response)
