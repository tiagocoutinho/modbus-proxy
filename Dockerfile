FROM python:3.8-alpine

COPY . /src

# install dependencies to the local user directory
RUN pip --disable-pip-version-check --no-input --no-cache-dir --timeout 3 \
    install src/[yaml]

# clean up
RUN rm /src -r

ENTRYPOINT ["modbus-proxy"]
CMD ["--help"]
