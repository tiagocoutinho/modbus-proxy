# first stage
FROM python:3.8 AS builder
COPY requirements.txt .

# install dependencies to the local user directory (eg. /root/.local)
RUN pip install --user -r requirements.txt

# second unnamed stage
FROM python:3.8-alpine
WORKDIR /code

# copy only the dependencies installation from the 1st stage image
COPY --from=builder /root/.local /root/.local
COPY modbus_proxy.py .

CMD python ./modbus_proxy.py -b "${MODBUS_BIND}" --modbus="${MODBUS_ADDRESS}"
