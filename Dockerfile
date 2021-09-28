FROM python:3.8-alpine

WORKDIR /src

COPY *.md modbus_proxy.py setup.py *.conf ./

# install dependencies to the local user directory
RUN pip --disable-pip-version-check --no-input --no-cache-dir --timeout 3 \
    install .[yaml]

# clean up
RUN rm *.md modbus_proxy.py setup.py

ENTRYPOINT ["modbus-proxy"]
CMD ["--help"]