#!/bin/bash

if [ -z "$1" ]; then
    echo "Usage: $0 <config_directory>"
    echo "Example: $0 config/solar_plant"
    exit 1
fi

CONFIG=$1

#if [ "$EUID" -ne 0 ]; then
#  echo "Please run this script with sudo:"
#  echo "sudo $0 <config_directory>"
#  exit 1
#fi


echo "ICS-SimLab STARTED"

echo "STOPPING PREVIOUS CONTAINERS"
docker compose down 2>/dev/null || true

echo "REMOVING PREVIOUS DIRECTORIES"
sudo rm -rf simulation 2>/dev/null || rm -rf simulation 2>/dev/null || true

docker system prune -f

echo "ACTIVATING ENVIRONMENT"
source .venv/bin/activate

echo "BUILDING SIMULATION FILES"
python3 main.py $1 || { echo "ERROR: Setup failed. Exiting."; exit 1; }

echo "DOCKER_COMPOSE BUILD"
docker compose build

echo "DOCKER_COMPOSE UP"
docker compose up
