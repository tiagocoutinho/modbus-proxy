# ModBus TCP proxy

[![ModBus TCP proxy](https://img.shields.io/pypi/v/modbus-proxy.svg)](https://pypi.python.org/pypi/modbus-proxy)


[![ModBus TCP proxy updates](https://pyup.io/repos/github/tiagocoutinho/modbus-proxy/shield.svg)](https://pyup.io/repos/github/tiagocoutinho/modbus-proxy/)

Many modbus devices support only one or very few clients. This proxy acts as a bridge between the client and the modbus device. This allows multiple clients to communicate with the same modbus device.

When multiple clients are connected, cross messages are avoided by serializing communication on a first come first served REQ/REP basis.

## Installation

From within your favorite python environment type:

`$ pip install modbus-proxy`

## Running the server

```console
$ modbus-proxy -b tcp://0:9000 --modbus tcp://plc1.acme.org:502
```

Now, instead of connecting your client(s) to `plc1.acme.org:502` you just need to
tell them to connect to `*machine*:9000` (where *machine* is the host where
modbus-proxy is running).

## Running the examples

To run the examples you will need to have
[umodbus](https://github.com/AdvancedClimateSystems/uModbus) installed (do it
with `pip install umodbus`).

Start the `simple_tcp_server.py` (this will simulate an actual modbus hardware):

```console
$ python examples/simple_tcp_server.py -b :5020
```

You can run the example client just to be sure direct communication works:

```console
$ python examples/simple_tcp_client.py -a 0:5020
holding registers: [1, 2, 3, 4]
```

Now for the real test:

Start a modbus-proxy bridge server with:

```console
$ modbus-proxy -b tcp://:9000 --modbus tcp://:5020
```

Finally run a the example client but now address the proxy instead of the server
(notice we are now using port *9000* and not *5020):

```console
$ python examples/simple_tcp_client.py -a 0:9000
holding registers: [1, 2, 3, 4]
```

## Credits

### Development Lead

* Tiago Coutinho <coutinhotiago@gmail.com>

### Contributors

None yet. Why not be the first?
