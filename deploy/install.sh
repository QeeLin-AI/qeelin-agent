#!/usr/bin/env bash
# crypto-agent 一键部署（Ubuntu 22.04 / 24.04，其他 Debian 系同理）
#
#   sudo APP_DIR=/opt/crypto-agent bash deploy/install.sh
#
# 做的事：建用户 → 建 venv 装依赖 → 生成访问令牌 → 写 systemd → 开机自启
# 不会碰你的原始目录，不会覆盖已有 .env。
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/crypto-agent}"
SERVICE_USER="${SERVICE_USER:-cryptotrader}"
# 服务账号是 nologin 的（不需要、也不允许 SSH 登录），
# 开隧道要用你自己的 sudo 账号或 root，否则 ssh 会直接被 nologin 拒掉。
OPS_USER="${OPS_USER:-${SUDO_USER:-root}}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APP_DIR}/.env"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "要用 root 跑：sudo bash deploy/install.sh"; exit 1; }

log "1/7 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip chrony curl git rsync ca-certificates

log "2/7 时间同步（签名时间戳偏差 >1s 会被币安拒单）"
systemctl enable --now chrony >/dev/null 2>&1 || true
chronyc makestep >/dev/null 2>&1 || true

log "3/7 创建用户 ${SERVICE_USER}"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -r -m -d "/home/${SERVICE_USER}" -s /usr/sbin/nologin "$SERVICE_USER"

log "4/7 同步代码到 ${APP_DIR}"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # 不带账本/缓存/虚拟环境
  rsync -a --delete \
    --exclude '__pycache__' --exclude '.venv' --exclude 'runs' --exclude 'dist' \
    --exclude 'data' --exclude 'docs/.edge*' --exclude '.env' \
    "$SRC_DIR"/ "$APP_DIR"/
fi
mkdir -p "$APP_DIR"/{runs,data,factors_library}
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

log "5/7 建 venv 装依赖"
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

log "6/7 写环境变量文件（含访问令牌）"
if [[ ! -f "$ENV_FILE" ]]; then
  TOKEN="$(openssl rand -hex 24)"
  cat > "$ENV_FILE" <<EOF
# crypto-agent 运行环境。权限 600，不要提交到 git。
CRYPTOAGENT_WEB_TOKEN=${TOKEN}
# --- 接真模型（改完 restart 生效）---
# CRYPTOAGENT_LLM_PROVIDER=openai
# CRYPTOAGENT_LLM_BASE_URL=https://api.deepseek.com/v1
# CRYPTOAGENT_LLM_MODEL=deepseek-chat
# OPENAI_API_KEY=sk-xxx
# --- 币安（默认不填 = 只能跑本地模拟）---
# BINANCE_API_KEY=
# BINANCE_API_SECRET=
EOF
  chown "$SERVICE_USER":"$SERVICE_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  已生成访问令牌，记下来：$(grep CRYPTOAGENT_WEB_TOKEN "$ENV_FILE" | cut -d= -f2)"
else
  # 手工放进来的 .env（从旧机搬过来的）：属主/权限也要修，否则服务读不到
  chown "$SERVICE_USER":"$SERVICE_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "  $ENV_FILE 已存在，保持不变"
fi

log "7/7 安装并启动 systemd 服务"
install -m 644 "$APP_DIR/deploy/cryptoagent.service" /etc/systemd/system/cryptoagent.service
systemctl daemon-reload
systemctl enable --now cryptoagent
sleep 2
systemctl --no-pager --lines=8 status cryptoagent || true

TOKEN_VAL="$(grep CRYPTOAGENT_WEB_TOKEN "$ENV_FILE" | cut -d= -f2)"
cat <<EOF

部署完成。控制台只监听 127.0.0.1，在你自己电脑上开隧道访问：

  ssh -N -L 8000:127.0.0.1:8000 ${OPS_USER}@<VPS_IP>

然后本机浏览器（Windows/Mac 都行，不需要服务器有桌面）：

  http://127.0.0.1:8000/?token=${TOKEN_VAL}

注意：${SERVICE_USER} 是 nologin 服务账号，只能 sudo -u 运行程序，不能用来 SSH。
想直接公网访问（和原来一样开浏览器输网址）：把 unit 里的 --host 改成 0.0.0.0，
再到轻量控制台放行 8000 端口即可；令牌已生成，代码会强制校验。

常用命令：
  systemctl status cryptoagent        # 看状态
  journalctl -u cryptoagent -f        # 看日志
  systemctl restart cryptoagent       # 改完 .env 重启
  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python -m cryptoagent netcheck
  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python -m cryptoagent llmtest

EOF
