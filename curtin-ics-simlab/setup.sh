#!/bin/bash

if [ "$EUID" -ne 0 ]; then
  echo "Please run this script with sudo:"
  echo "sudo $0"
  exit 1
fi

echo "ICS-SimLab SETUP STARTED"

sudo apt update && 

echo "INSTALLING TOOLS"
if ! command -v socat &> /dev/null; then
    echo "socat is not installed. Installing..."
    sudo apt install -y socat
fi

if ! command -v tshark &> /dev/null; then
    echo "tshark is not installed. Installing..."
    sudo apt install -y tshark
fi

echo " INSTALL DUMPCAP"
if [ ~ -d "/usr/bin/dumpcap" ]; then
    sudo apt update && sudo apt install -y wireshark
fi

echo "INSTALL PIP"
sudo apt install pip

echo ""
sudo chmod +x /usr/bin/dumpcap

echo "Revoking sudo credentials..."
sudo -k

echo "DOWN PREVIOUS CONTAINERS"
docker compose down 

echo "PRUNING DOCKER"
docker system prune -f

echo "INSTALL PYTHON VIRTUAL ENVIRONMENT"
sudo apt install python3.12-venv



echo "CREATING PYTHON ENVIRONMENT"
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

echo "ACTIVATING ENVIRONMENT AND INSTALLING REQUIREMENTS"
source .venv/bin/activate
pip3 install -r requirements.txt
