FROM alpine:edge

MAINTAINER Sidesplitter, https://github.com/SexualRhinoceros/MusicBot

# Requirements
COPY requirements.txt /app/requirements.txt
RUN apk add --no-cache build-base libintl python3 python3-dev ffmpeg opus opus-dev libffi libffi-dev rtmpdump ca-certificates libsodium libsodium-dev pkgconf && \
	SODIUM_INSTALL=system pip3 install -r /app/requirements.txt && \
	apk del build-base opus-dev libffi-dev libsodium-dev

# Workdir
WORKDIR /app

# Add code
COPY . /app
RUN python3 setup.py install

# Config volume
VOLUME /app/config

CMD ["python3", "musicbot"]
