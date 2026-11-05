#!/bin/bash
# 将 garmin 项目同步到飞牛OS (FnOS) SMB 共享
# 用法: bash sync_to_fnos.sh
set -e

SRC="/Users/andy/Documents/garmin"
SHARE="Download"                      # 飞牛OS SMB 共享名
FNOS_IP="10.10.200.20"
FNOS_USER="eser3000"
MNT="/Volumes/${SHARE}"               # 本地挂载点
DST="${MNT}/garmin"                   # 飞牛OS 上的目标目录

# 若共享未挂载则尝试挂载（已挂载则跳过）
if ! mount | grep -q "on ${MNT} (smbfs"; then
  echo "挂载 ${SHARE} (${FNOS_IP}) ..."
  mkdir -p "${MNT}"
  mount_smbfs "//${FNOS_USER}@${FNOS_IP}/${SHARE}" "${MNT}"
fi

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
