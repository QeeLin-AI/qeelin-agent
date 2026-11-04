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

cd "$APP_DIR"

# 运行时参数由设置页改写，不能被仓库版本盖掉
sudo -u "$SERVICE_USER" git update-index --skip-worktree config/default.json 2>/dev/null || true

sudo -u "$SERVICE_USER" git fetch --prune origin
sudo -u "$SERVICE_USER" git reset --hard origin/main

sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r requirements.txt

# unit 文件也会随代码更新（环境变量、重启策略、加固项），必须一并同步，
# 否则代码更新了、systemd 还在跑老配置
install -m 644 deploy/cryptoagent.service /etc/systemd/system/cryptoagent.service
systemctl daemon-reload

systemctl restart cryptoagent
sleep 2
systemctl --no-pager --lines=10 status cryptoagent || true
