FROM ubuntu:16.04

MAINTAINER Sidesplitter, https://github.com/SexualRhinoceros/MusicBot

# Requirements
ADD requirements.txt /app/requirements.txt
RUN DEBIAN_FRONTEND=noninteractive apt-get update && \
	apt-get install build-essential unzip python3 python3-dev python3-pip ffmpeg libopus-dev libffi-dev rtmpdump -y && \
	pip3 install -r /app/requirements.txt && \
	rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Workdir
WORKDIR /app

# Add code
ADD . /app
RUN python3 setup.py install

# Config volume
VOLUME /app/config

CMD python3 musicbot
