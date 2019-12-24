#!/bin/bash

docker run \
    --mount type=bind,source="$(pwd)"/config,target=/config \
    --mount type=bind,source="$(pwd)"/downloads,target=/downloads \
    mguterl/redditdownloader
