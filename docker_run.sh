#!/bin/bash

docker run -d -t \
    --mount type=bind,source="$(pwd)"/config,target=/config \
    --mount type=bind,source="$(pwd)"/downloads,target=/downloads \
    mguterl/redditdownloader
