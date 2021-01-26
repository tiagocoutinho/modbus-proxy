# ModBus TCP proxy

[![ModBus proxy][pypi-version]](https://pypi.python.org/pypi/modbus-proxy)
[![Python Versions][pypi-python-versions]](https://pypi.python.org/pypi/modbus-proxy)
[![Pypi status][pypi-status]](https://pypi.python.org/pypi/modbus-proxy)
![License][license]

Many modbus devices support only one or very few clients. This proxy acts as a bridge between the client and the modbus device. It can be seen as a
layer 7 reverse proxy.
This allows multiple clients to communicate with the same modbus device.

When multiple clients are connected, cross messages are avoided by serializing communication on a first come first served REQ/REP basis.

## Installation

From within your favorite python 3 environment type:

`$ pip install modbus-proxy`

Note: On some systems `pip` points to a python 2 installation.
You might need to use `pip3` command instead.

Additionally, if you want logging configuration:
* YAML: `pip install modbus-proxy[yaml]` (see below)
* TOML: `pip install modbus-proxy[toml]` (see below)

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

## Docker

This project ships with a basic [Dockerfile](./Dockerfile) which you can use
as a base to launch modbus-proxy inside a docker container.

First, build the docker image with:

```console
$ docker build -t img-modbus-proxy .
```

To run the container you need to set two variables:

* *MODBUS_ADDRESS* - the address of your modbus device (ex: `tcp://plc.acme.org:502`)
* *MODBUS_BIND* - which TCP address your server will bind to *inside docker* (ex: `tcp://0:502`)

Also you need to expose the MODBUS_BIND port.

Optionally, you can set *MODBUS_LOG* to `log.conf`, `log-verbose.conf`.

Here is a minimum example:

```
$ docker run -d -p 5020:502 -e MODBUS_BIND=tcp://0:502 -e MODBUS_ADDRESS=tcp://plc.acme.org:502 img-modbus-proxy
```

Should be able to access your modbus device through the modbus-proxy by connecting your client(s) to `<your-hostname/ip>:5020`

## Advanced configuration

#### `--timeout=<time in secs>`

Defines modbus timeout for establishing a connection and for making REQ/REP. Time is given in seconds (or fractions of). Defaults to 10s.

#### `--log-config-file`

Logging configuration file.

If a relative path is given, it is relative to the current working directory.


If a `.conf` or `.ini` file is given, it is passed directly to
[logging.config.fileConfig()](https://docs.python.org/library/logging.config.html#logging.config.fileConfig) so the file contents must
obey the
[Configuration file format](https://docs.python.org/library/logging.config.html#configuration-file-format).

A simple logging configuration (also available at [log.conf](./log.conf))
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
[log-verbose.conf](./log-verbose.conf)


If a `.yml`, `.yaml`, or `.toml` file is given, it is parsed as a YAML or TOML
file respectively. The result is then passed to
[logging.config.dictConfig()](https://docs.python.org/library/logging.config.html#logging.config.dictConfig) so the file contents
must obey the
[Configuration dictionary schema](https://docs.python.org/library/logging.config.html#configuration-dictionary-schema).

The same example above (also available at [log.yml](./log.yml)) can be achieved in YAML with:

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
