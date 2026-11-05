#!/bin/bash
# 将 garmin 项目同步到飞牛OS (FnOS) SMB 共享
# 用法: bash sync_to_fnos.sh
set -e

SRC="/Users/andy/Documents/garmin"
SHARE="Download"                      # 飞牛OS SMB 共享名
FNOS_IP="10.10.200.20"
FNOS_USER="eser3000"
VOL_MNT="/Volumes/${SHARE}"           # Finder/系统已挂载时优先使用
OWN_MNT="${HOME}/fnos/${SHARE}"       # 未挂载时挂到用户可写目录（/Volumes 需 root）

# ① 已有挂载直接用（无需密码、无需建目录）
if mount | grep -q "on ${VOL_MNT} (smbfs"; then
  MNT="${VOL_MNT}"
else
  MNT="${OWN_MNT}"
  mkdir -p "${MNT}"
  if ! mount | grep -q "on ${MNT} (smbfs"; then
    echo "挂载 //${FNOS_USER}@${FNOS_IP}/${SHARE} -> ${MNT}（需输入密码）..."
    if ! mount_smbfs "//${FNOS_USER}@${FNOS_IP}/${SHARE}" "${MNT}"; then
      echo "挂载失败：请确认飞牛OS(${FNOS_IP})在线且 SMB 可达，或先在 Finder 中挂载共享" >&2
      exit 1
    fi
  fi
fi
DST="${MNT}/garmin"                   # 飞牛OS 上的目标目录

echo "同步 ${SRC} -> ${DST}"
rsync -a --delete \
  --exclude='.venv' \
  --exclude='.codebuddy' \
  --exclude='.pytest_cache' \
  --exclude='__pycache__' \
  --exclude='.DS_Store' \
  --exclude='*.pyc' \
  "${SRC}/" "${DST}/"

echo "完成: 已同步到飞牛OS ${DST}"
