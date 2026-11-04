#!/usr/bin/env bash
# 服务器上更新代码并重启（Linux 部署用）
#
#   sudo bash deploy/update.sh
#
# 只动代码与依赖。config/default.json、.env、runs/、data/、factors_library/
# 都是运行数据，不会被覆盖、也不会被删除：
#   - .env / runs / data / factors_library 在 .gitignore 里，git 根本看不见；
#   - config/default.json 虽然被仓库跟踪，但这里先打了 skip-worktree 标记，
#     reset 时 git 会跳过它，保留服务器上的运行时参数。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/crypto-agent}"
SERVICE_USER="${SERVICE_USER:-cryptotrader}"
SERVICE_NAME="${SERVICE_NAME:-qeelin-agent}"
# 2026-11-05 之前的旧 unit 名，新 unit 用 Alias 继续兼容它
LEGACY_SERVICE="${LEGACY_SERVICE:-cryptoagent}"

cd "$APP_DIR"

# 运行时参数由设置页改写，不能被仓库版本盖掉
sudo -u "$SERVICE_USER" git update-index --skip-worktree config/default.json 2>/dev/null || true

sudo -u "$SERVICE_USER" git fetch --prune origin
sudo -u "$SERVICE_USER" git reset --hard origin/main

sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r requirements.txt

# unit 文件也会随代码更新（环境变量、重启策略、加固项），必须一并同步，
# 否则代码更新了、systemd 还在跑老配置
#
# 先清理老机器上残留的真实 unit 文件：它和新 unit 的 Alias 同名，留着会冲突。
# 必须排除软链 —— 新 unit enable 之后 /etc/systemd/system/cryptoagent.service
# 本身就是指向 qeelin-agent.service 的 Alias 软链，误删就把兼容入口弄丢了。
if [ -f "/etc/systemd/system/${LEGACY_SERVICE}.service" ] \
   && [ ! -L "/etc/systemd/system/${LEGACY_SERVICE}.service" ]; then
  echo "  迁移旧 unit：${LEGACY_SERVICE} -> ${SERVICE_NAME}"
  systemctl stop "${LEGACY_SERVICE}" 2>/dev/null || true
  systemctl disable "${LEGACY_SERVICE}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${LEGACY_SERVICE}.service"
fi

install -m 644 "deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

# enable 不只是开机自启：Alias 软链也要靠这一步生成
systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || true

systemctl restart "${SERVICE_NAME}"
sleep 2
systemctl --no-pager --lines=10 status "${SERVICE_NAME}" || true
