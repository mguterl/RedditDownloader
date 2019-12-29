#!/bin/bash
set -e

# allow schedule to be set at container runtime
# default to every day at 4am
SCHEDULE=${SCHEDULE:-"0 4 * * *"}

echo "$SCHEDULE /usr/local/bin/python /Run.py --settings /config/settings.json >> /var/log/cron.log 2>&1" > /etc/cron.d/redditdownloader
chmod 0644 /etc/cron.d/redditdownloader
crontab /etc/cron.d/redditdownloader

# touch the cron.log so that tail can follow it
# https://stackoverflow.com/a/43807880
touch /var/log/cron.log

cron 
exec tail -f /var/log/cron.log
