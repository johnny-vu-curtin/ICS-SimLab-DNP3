FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --upgrade pip

# dnp3-python (VOLTTRON/PNNL) — tested on Ubuntu 22.04 + Python 3.11
# Do NOT use pydnp3 — abandoned since 2018, incompatible with Python 3.7+
RUN pip3 install dnp3-python==0.3.0b2
RUN pip3 install flask==3.1.0
RUN pip3 install numpy==1.26.1

ENV TERM=xterm-256color
ENV FLASK_ENV=production

WORKDIR /src
COPY ./src/ .
