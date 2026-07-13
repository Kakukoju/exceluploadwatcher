#!/bin/bash
set -e

MOUNT_POINT="/mnt/reagent_rd"
SHARE="//fls341/Reagent RD"
CRED_FILE="/home/harryhrguo/.smb-credentials"

echo "=== Installing cifs-utils (if not already) ==="
sudo apt-get update -qq
sudo apt-get install -y cifs-utils

echo "=== Creating mount point ==="
sudo mkdir -p "$MOUNT_POINT"

echo "=== Adding to /etc/fstab (auto mount on boot) ==="
FSTAB_LINE="${SHARE} ${MOUNT_POINT} cifs credentials=${CRED_FILE},iocharset=utf8,vers=3.0,uid=1001,gid=1001,file_mode=0644,dir_mode=0755,nofail,_netdev 0 0"

if ! grep -qF "$MOUNT_POINT" /etc/fstab; then
    echo "$FSTAB_LINE" | sudo tee -a /etc/fstab
    echo "Added fstab entry."
else
    echo "fstab entry already exists, skipping."
fi

echo "=== Mounting ==="
sudo mount "$MOUNT_POINT"

echo "=== Verifying ==="
ls "$MOUNT_POINT" | head -10

echo "=== Done! Share mounted at ${MOUNT_POINT} ==="
echo "Target folder: ${MOUNT_POINT}/0. Tutti/生產給線"
