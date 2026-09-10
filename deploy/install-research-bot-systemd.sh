#!/usr/bin/env bash
# Run this script from an interactive SSH terminal with sudo access.
set -euo pipefail

readonly NEWS=/home/seiya/services/news-bot/.systemd
readonly ARXIV=/home/seiya/services/arxiv-bot/.systemd
readonly UNITS=(
  research-bots-news-collect.service
  research-bots-news-collect.timer
  research-bots-news-daily.service
  research-bots-news-daily.timer
  research-bots-backup.service
  research-bots-backup.timer
  research-bots-arxiv-daily.service
  research-bots-arxiv-daily.timer
  research-bots-arxiv-reactions.service
  research-bots-arxiv-reactions.timer
)
readonly TIMERS=(
  research-bots-news-collect.timer
  research-bots-news-daily.timer
  research-bots-backup.timer
  research-bots-arxiv-daily.timer
  research-bots-arxiv-reactions.timer
)

for unit in "${UNITS[@]}"; do
  case "$unit" in
    research-bots-arxiv-*) source="$ARXIV/$unit" ;;
    *) source="$NEWS/$unit" ;;
  esac
  sudo install -m 0644 "$source" "/etc/systemd/system/$unit"
done

sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/research-bots-*.service /etc/systemd/system/research-bots-*.timer
sudo systemctl enable --now "${TIMERS[@]}"
systemctl list-timers --all 'research-bots-*' --no-pager
