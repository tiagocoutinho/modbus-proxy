"""
Bridge

device = reader, writer

"""
import asyncio
import logging
import urllib.parse

log = logging.getLogger("modbus-proxy")


def parse_url(url):
    if "://" not in url:
        url = f"tcp://{url}"
    result = urllib.parse.urlparse(url)
    if not result.hostname:
        url = result.geturl().replace("://", "://0")
        result = urllib.parse.urlparse(url)
    return result


async def read_message(reader) -> bytes:
    """Read ModBus TCP message"""
    # TODO: Handle Modbus RTU and ASCII
    header = await reader.readexactly(6)
    size = int.from_bytes(header[4:], "big")
    message = header + await reader.readexactly(size)
    log.debug("received %r", message)
    return message


async def message_stream(reader):
    while True:
        try:
            yield await read_message(reader)
        except asyncio.IncompleteReadError as error:
            if error.partial:
                log.error("reading error: %r", error)
            else:
                log.info("client closed connection")
            return
        except Exception as error:
            log.error("reading error: %r", error)
            return


async def queue_stream(queue):
    while True:
        yield await queue.get()


async def tcp_server(host, port, callback):
    server = await asyncio.start_server(callback, host, port, start_serving=True)
    log.info("Ready to accept requests on %s", server.sockets[0].getsockname())
    return server


async def tcp_connect(host, port):
    reader, writer = await asyncio.open_connection(host, port)
    log.info("Connected to %s", writer.get_extra_info("peername"))
    return reader, writer


async def write_message(writer, message):
    writer.write(message)
    await writer.drain()


async def bridge(config):
    bind = parse_url(config["listen"]["bind"])
    host = bind.hostname
    port = 502 if bind.port is None else bind.port
    send_modbus_queue = asyncio.Queue(maxsize=1000)
    recv_modbus_queue = asyncio.Queue(maxsize=1000)

    modbus_url = parse_url(config["modbus"]["url"])
    modbus_reader, modbus_writer = await tcp_connect(modbus_url.hostname, modbus_url.port)

    async def handle_client(reader, writer):
        async for message in message_stream(reader):
            await send_modbus_queue.put((message, writer))
        
    async def client_to_modbus():
        async for message, from_ in queue_stream(send_modbus_queue):
            await recv_modbus_queue.put(from_)
            await write_message(modbus_writer, message)

    async def modbus_to_client():
        async for writer in queue_stream(recv_modbus_queue):
            message = await read_message(modbus_reader)
            await write_message(writer, message)

    server = await tcp_server(host, port, handle_client)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(client_to_modbus())
        tg.create_task(modbus_to_client())
        tg.create_task(server.serve_forever())


def bridges(config):
    return [bridge(device) for device in config["devices"]]


from modbus_proxy import parse_args, create_config
async def run(args=None, ready=None):
    args = parse_args(args)
    config = create_config(args)
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(bridge) for bridge in bridges(config)]


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.warning("Ctrl-C pressed. Bailing out!")


if __name__ == "__main__":
    main()