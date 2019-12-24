#!/bin/bash

# DOCKER_BUILDKIT explanation: https://stackoverflow.com/a/55280541
DOCKER_BUILDKIT=1 docker build -t mguterl/redditdownloader .
