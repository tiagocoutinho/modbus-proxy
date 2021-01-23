FROM python:3.8-alpine

WORKDIR /src

COPY *.md modbus_proxy.py setup.py ./

# install dependencies to the local user directory
RUN pip install .

# clean up
RUN rm *.md modbus_proxy.py setup.py

CMD modbus-proxy -b "${MODBUS_BIND}" --modbus="${MODBUS_ADDRESS}"
