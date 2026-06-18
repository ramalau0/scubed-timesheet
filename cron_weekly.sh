#!/bin/bash
# Weekly cron job — runs every Monday at 08:00
# Add to crontab with:  crontab -e
#   0 8 * * 1 /home/ramalau/Documents/projects/personal/scubed-timesheet/cron_weekly.sh

set -e
cd "$(dirname "$0")"
source .venv/bin/activate
python timesheet_bot.py create >> logs/timesheet.log 2>&1
