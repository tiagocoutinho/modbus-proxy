# ModBus TCP proxy

[![ModBus proxy][pypi-version]](https://pypi.python.org/pypi/modbus-proxy)
[![Python Versions][pypi-python-versions]](https://pypi.python.org/pypi/modbus-proxy)
[![Pypi status][pypi-status]](https://pypi.python.org/pypi/modbus-proxy)
![License][license]
[![CI][CI]](https://github.com/tiagocoutinho/modbus-proxy/actions/workflows/ci.yml)

Many modbus devices support only one or very few clients. This proxy acts as a bridge between the client and the modbus device. It can be seen as a
layer 7 reverse proxy.
This allows multiple clients to communicate with the same modbus device.

When multiple clients are connected, cross messages are avoided by serializing communication on a first come first served REQ/REP basis.

## Installation

From within your favorite python 3 environment type:

`$ pip install modbus-proxy[all]`

Advanced installation for a specific environments:

* YAML: `pip install modbus-proxy[yaml]`
* TOML: `pip install modbus-proxy[toml]`
* serial line support: `pip install modbus-proxy[serial]`

## Running the server

First, you will need to write a configuration file where you specify for each
modbus device you which to control:

* modbus connection (the modbus device url)
* listen interface (to which url your clients should connect)

Configuration files can be written in YAML (*.yml* or *.yaml*) or TOML (*.toml*).

Suppose you have a PLC modbus device listening on *plc1.acme.org:502* and you
want your clients to connect to your machine on port 9000. A YAML configuration
would look like this:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502     # device url (mandatory). This will assume modbus TCP over TCP
    timeout: 10                # communication timeout (s) (optional, default: 10)
    connection_time: 0.1       # delay after connection (s) (optional, default: 0)
  listen:
    bind: 0:9000               # listening address (mandatory)
```

Assuming you saved this file as `modbus-proxy-config.yml`, start the server 
with:

```bash
$ modbus-proxy -c ./modbus-proxy-config.yml
```

Now, instead of connecting your client(s) to `plc1.acme.org:502` you just need to
tell them to connect to `*machine*:9000` (where *machine* is the host where
modbus-proxy is running).

Note that the server is able to handle multiple incoming clients on port 9000.
Each client request will be served on a FIFO fashion.

The server is also capable of handling multiple modbus devices. Here is a
configuration example for 2 devices:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502       # assumes modbus TCP over TCP connection (*)
  listen:
    bind: 0:9000

- modbus:
    url: serial:///dev/ttyS1     # assume modbus RTU over serial line (**) 
  listen:
    bind: 0:9001

# (*) Use tcp+rtu://<host>:<port> for modbus RTU over TCP socket. Useful when configuring
#     ser2net in raw mode 
# (**) Use serial+tcp:// scheme if you wish to force modbus TCP over serial line.
#      Use rfc2217://<host>:<port> for modbus RTU over ser2net. Useful when configuring
#      ser2net in telnet mode 
```

If you have a *single* modbus device, you can avoid writting a configuration file by
providing all arguments in the command line:

```bash
modbus-proxy -b tcp://0:9000 --modbus tcp://plc1.acme.org:502
```

(hint: run `modbus-proxy --help` to see all available options)


## Running the examples

To run the examples you will need to have
[umodbus](https://github.com/AdvancedClimateSystems/uModbus) installed (do it
with `pip install umodbus`).

Start the `simple_tcp_server.py` (this will simulate an actual modbus hardware
handling modbus TCP protocol):

```bash
$ python examples/simple_tcp_server.py
```

Start the `simple_tcp_server.py` (this will simulate an actual modbus hardware
handling modbus RTU protocol):

```bash
$ python examples/simple_rtu_server.py
```

You can run the example client just to be sure direct communication works:

```bash
$ python examples/simple_tcp_client.py -a localhost:5020
holding registers: [1, 2, 3, 4]
```

Now for the real test:

Start a modbus-proxy bridge server with:

```bash
$ modbus-proxy examples/modbus-proxy-config.yml
```

Finally run a the example client but now address the proxy instead of the server
(notice we are now using port *5021* and not *5020*):

```bash
$ python examples/simple_tcp_client.py -a localhost:5021
holding registers: [1, 2, 3, 4]
```

The modbus RTU should also work:

```bash
$ python examples/simple_rtu_client.py -a localhost:5022
holding registers: [1, 2, 3, 4]
```

## Docker

This project ships with a basic [Dockerfile](./Dockerfile) which you can use
as a base to launch modbus-proxy inside a docker container.

First, build the docker image with:

```bash
$ docker build -t modbus-proxy .
```

To bridge a single modbus device without needing a configuration file is
straight forward:

```bash
$ docker run -d -p 5020:502 modbus-proxy -b tcp://0:502 --modbus tcp://plc1.acme.org:502
```

Now you should be able to access your modbus device through the modbus-proxy by
connecting your client(s) to `<your-hostname/ip>:5020`.

If, instead, you want to use a configuration file, you must mount the file so
it is visible by the container.

Assuming you have prepared a `conf.yml` in the current directory:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:502
```

Here is an example of how to run the container:

```bash
docker run -p 5020:502 -v $PWD/conf.yml:/src/conf.yml modbus-proxy -c /src/conf.yml
```

Note that for each modbus device you add in the configuration file you need
to publish the corresponding bind port on the host
(`-p <host port>:<container port>` argument).

## Logging configuration

Logging configuration can be added to the configuration file by adding a new `logging` keyword.

The logging configuration will be passed to
[logging.config.dictConfig()](https://docs.python.org/library/logging.config.html#logging.config.dictConfig)
so the file contents must obey the
[Configuration dictionary schema](https://docs.python.org/library/logging.config.html#configuration-dictionary-schema).

Here is a YAML example:

```yaml
devices:
- modbus:
    url: plc1.acme.org:502
  listen:
    bind: 0:9000
logging:
  version: 1
  formatters:
    standard:
      format: "%(asctime)s %(levelname)8s %(name)s: %(message)s"
  handlers:
    console:
      class: logging.StreamHandler
      formatter: standard
  root:
    handlers: ['console']
    level: DEBUG
```

### `--log-config-file` (deprecated)

Logging configuration file.

If a relative path is given, it is relative to the current working directory.

If a `.conf` or `.ini` file is given, it is passed directly to
[logging.config.fileConfig()](https://docs.python.org/library/logging.config.html#logging.config.fileConfig) so the file contents must
obey the
[Configuration file format](https://docs.python.org/library/logging.config.html#configuration-file-format).

A simple logging configuration (also available at [log.conf](examples/log.conf))
which mimics the default configuration looks like this:

```toml
[formatters]
keys=standard

[handlers]
keys=console

[loggers]
keys=root

[formatter_standard]
format=%(asctime)s %(levelname)8s %(name)s: %(message)s

[handler_console]
class=StreamHandler
formatter=standard

[logger_root]
level=INFO
handlers=console
```

A more verbose example logging with a rotating file handler:
[log-verbose.conf](examples/log-verbose.conf)

The same example above (also available at [log.yml](examples/log.yml)) can be achieved in YAML with:

```yaml
version: 1
formatters:
  standard:
    format: "%(asctime)s %(levelname)8s %(name)s: %(message)s"
handlers:
  console:
    class: logging.StreamHandler
    formatter: standard
root:
  handlers: ['console']
  level: DEBUG
```


## Credits

### Development Lead

* Tiago Coutinho <coutinhotiago@gmail.com>

### Contributors

None yet. Why not be the first?

[pypi-python-versions]: https://img.shields.io/pypi/pyversions/modbus-proxy.svg
[pypi-version]: https://img.shields.io/pypi/v/modbus-proxy.svg
[pypi-status]: https://img.shields.io/pypi/status/modbus-proxy.svg
[license]: https://img.shields.io/pypi/l/modbus-proxy.svg
[CI]: https://github.com/tiagocoutinho/modbus-proxy/actions/workflows/ci.yml/badge.svg
