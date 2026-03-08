ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache bash python3 py3-pip

COPY . /app

WORKDIR /app

RUN pip3 install --break-system-packages lifxlan

CMD ["/bin/bash", "/app/run.sh"]
