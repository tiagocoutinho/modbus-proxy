import argparse
import collections
import socketserver

from umodbus.server.tcp import RequestHandler, get_server


class Server(socketserver.TCPServer):
    allow_reuse_address = True


# Parse command line arguments
parser = argparse.ArgumentParser()
parser.add_argument("-b", "--bind", default="127.0.0.1:5020")
args = parser.parse_args()
host, port = args.bind.rsplit(":", 1)
port = int(port)

app = get_server(Server, (host, port), RequestHandler)
# A very simple data store which maps addresses against their values.
data_store = collections.defaultdict(int)


@app.route(slave_ids=[1], function_codes=[3, 4], addresses=list(range(0, 10)))
def read_data_store(slave_id, function_code, address):
    """ " Return value of address."""
    return data_store[address]


@app.route(slave_ids=[1], function_codes=[6, 16], addresses=list(range(0, 10)))
def write_data_store(slave_id, function_code, address, value):
    """ " Set value for address."""
    data_store[address] = value


try:
    app.serve_forever()
finally:
    app.shutdown()
    app.server_close()
