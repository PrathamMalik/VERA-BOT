#!/bin/sh
# restart the local bot in the background (dev helper)
cd "$(dirname "$0")/.."
[ -f /tmp/claude-0/bot.pid ] && kill $(cat /tmp/claude-0/bot.pid) 2>/dev/null
sleep 0.5
QUIET=1 nohup python3 bot.py > /tmp/claude-0/bot.log 2>&1 &
echo $! > /tmp/claude-0/bot.pid
sleep 1.2
