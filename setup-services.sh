#!/bin/bash
set -e

echo "=== Installing system packages ==="
sudo apt-get update -qq
sudo apt-get install -y python3.14-venv python3-dev

echo "=== Creating venv ==="
python3 -m venv /home/harryhrguo/local-watcher/.venv
/home/harryhrguo/local-watcher/.venv/bin/pip install --upgrade pip
/home/harryhrguo/local-watcher/.venv/bin/pip install watchdog requests schedule openpyxl

echo "=== Enabling lingering (services start on boot without login) ==="
sudo loginctl enable-linger harryhrguo

echo "=== Reloading and enabling services ==="
systemctl --user daemon-reload
systemctl --user enable --now watch-assayprocess.service
systemctl --user enable --now production-plan-watcher.service
systemctl --user enable --now beads-inventory-monitor.service
systemctl --user enable --now excel-uploader.service
systemctl --user enable --now tutti-pur-baseline-watcher.service

echo "=== Done! Status: ==="
systemctl --user status watch-assayprocess production-plan-watcher beads-inventory-monitor excel-uploader tutti-pur-baseline-watcher --no-pager || true
