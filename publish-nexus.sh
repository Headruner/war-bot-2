#!/bin/bash

# Nexus Deployment Script
# Instructions:
# 1. Set your Nexus URL and Credentials
# 2. Run this script to build and push the image

NEXUS_URL="YOUR_NEXUS_URL"
NEXUS_USER="YOUR_USERNAME"
NEXUS_PASS="YOUR_PASSWORD"
IMAGE_NAME="eve-war-bot-v2"
TAG=$(date +%Y%m%d%H%M%S)

echo "Building image: $IMAGE_NAME:$TAG..."
docker build -t $IMAGE_NAME:$TAG .

echo "Logging into Nexus..."
echo $NEXUS_PASS | docker login -u $NEXUS_USER -p - $NEXUS_URL

echo "Pushing to Nexus..."
docker push $IMAGE_NAME:$TAG

echo "Deployment complete. Image $IMAGE_NAME:$TAG is now available on Nexus."
