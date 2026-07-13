#!/bin/bash
set -e

MOUNT_POINT="/mnt/mbbu_fab"
SHARE="//fls341/MBBU_FAB"
CRED_FILE="/home/harryhrguo/.smb-credentials"

echo "=== Installing cifs-utils ==="
sudo apt-get update -qq
sudo apt-get install -y cifs-utils

echo "=== Creating credentials file ==="
echo "請輸入 SMB 帳號:"
read -r SMB_USER
echo "請輸入 SMB 密碼:"
read -rs SMB_PASS
echo

cat > "$CRED_FILE" <<EOF
username=${SMB_USER}
password=${SMB_PASS}
EOF
chmod 600 "$CRED_FILE"

echo "=== Creating mount point ==="
sudo mkdir -p "$MOUNT_POINT"

echo "=== Adding to /etc/fstab (auto mount on boot) ==="
FSTAB_LINE="${SHARE} ${MOUNT_POINT} cifs credentials=${CRED_FILE},iocharset=utf8,uid=1001,gid=1001,file_mode=0644,dir_mode=0755,nofail,_netdev 0 0"

if ! grep -qF "$MOUNT_POINT" /etc/fstab; then
    echo "$FSTAB_LINE" | sudo tee -a /etc/fstab
fi

echo "=== Mounting ==="
sudo mount "$MOUNT_POINT"

echo "=== Verifying ==="
ls "$MOUNT_POINT" | head -10

echo "=== Done! Share mounted at ${MOUNT_POINT} ==="
