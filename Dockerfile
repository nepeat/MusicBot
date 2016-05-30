FROM ubuntu:16.04

MAINTAINER Sidesplitter, https://github.com/SexualRhinoceros/MusicBot

# Deps
RUN apt-get update \
    && apt-get install build-essential unzip python3 python3-dev python3-pip ffmpeg libopus-dev libffi-dev -y

# Workdir
WORKDIR /app

# Install dependencies
ADD requirements.txt /app/requirements.txt
RUN pip3 install -r /app/requirements.txt

# Add code
ADD . /app
RUN python3 setup.py install

# Config volume
VOLUME /app/config

CMD python3 musicbot
