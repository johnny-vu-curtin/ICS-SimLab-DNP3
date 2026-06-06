#!/bin/bash

if [ -z "$1" ]; then
    echo "Usage: sudo $0 <config_directory>"
    exit 1
fi

#if [ "$EUID" -ne 0 ]; then
#  echo "Please run this script with sudo:"
#  echo "sudo $0 <config_directory>"
#  exit 1
#fi


echo "ICS-SimLab STARTED"

echo "REMOVING PREVIOUS DIRECTORIES"
rm -r simulation

#echo "Revoking sudo credentials..."
#sudo -k

docker system prune -f

echo "ACTIVATING ENVIRONMENT"
source .venv/bin/activate

echo "BUILDING SIMULATION FILES"
python3 main.py $1

echo "DOCKER_COMPOSE BUILD"
docker compose build 

echo "DOCKER_COMPOSE UP"
docker compose up
