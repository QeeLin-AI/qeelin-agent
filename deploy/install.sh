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
# systemd 服务名跟着对外品牌走（QeeLin-Agent）；Python 包名仍是 cryptoagent，
# 改包名要动全仓 import 与部署路径，所以 ExecStart 里保持 -m cryptoagent。
SERVICE_NAME="${SERVICE_NAME:-qeelin-agent}"
# 服务账号是 nologin 的（不需要、也不允许 SSH 登录），
# 开隧道要用你自己的 sudo 账号或 root，否则 ssh 会直接被 nologin 拒掉。
OPS_USER="${OPS_USER:-${SUDO_USER:-root}}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${APP_DIR}/.env"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "要用 root 跑：sudo bash deploy/install.sh"; exit 1; }

log "1/8 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip chrony curl git rsync ca-certificates

log "2/8 时间同步（签名时间戳偏差 >1s 会被币安拒单）"
systemctl enable --now chrony >/dev/null 2>&1 || true
chronyc makestep >/dev/null 2>&1 || true

log "3/8 创建用户 ${SERVICE_USER}"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd -r -m -d "/home/${SERVICE_USER}" -s /usr/sbin/nologin "$SERVICE_USER"

log "4/8 同步代码到 ${APP_DIR}"
mkdir -p "$APP_DIR"
if [[ "$SRC_DIR" != "$APP_DIR" ]]; then
  # 不带账本/缓存/虚拟环境
  rsync -a --delete \
    --exclude '__pycache__' --exclude '.venv' --exclude 'runs' --exclude 'dist' \
    --exclude 'data' --exclude 'docs/.edge*' --exclude '.env' \
    --exclude 'config/default.json' \
    "$SRC_DIR"/ "$APP_DIR"/
fi
mkdir -p "$APP_DIR"/{runs,data,factors_library}
# 首次部署：没有运行时配置就从模板复制一份；已有配置绝不覆盖
# （default.json 是设置页会改写的运行时文件，不在仓库里跟踪）。
if [[ ! -f "$APP_DIR/config/default.json" && -f "$APP_DIR/config/default.example.json" ]]; then
  cp "$APP_DIR/config/default.example.json" "$APP_DIR/config/default.json"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

log "5/8 建 venv 装依赖"
sudo -u "$SERVICE_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

log "6/8 写环境变量文件（含访问令牌）"
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

log "7/8 安装并启动 systemd 服务"
install -m 644 "$APP_DIR/deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
sleep 2
systemctl --no-pager --lines=8 status "${SERVICE_NAME}" || true

log "8/8 安装 fail2ban（SSH 爆破防护）"
# 配置里覆盖了 journalmatch：Debian/Ubuntu 的 SSH 单元叫 ssh.service，
# 而 fail2ban 默认找 sshd.service，不覆盖就永远匹配不到日志（jail 空转）。
apt-get install -y -qq fail2ban
install -m 644 -o root -g root "$APP_DIR/deploy/fail2ban/qeelin.conf" /etc/fail2ban/jail.d/qeelin.local
systemctl enable --now fail2ban
systemctl restart fail2ban
sleep 2
fail2ban-client status sshd 2>/dev/null | grep -E "Journal matches|Currently banned" || \
  echo "  fail2ban 未就绪，检查 journalctl -u fail2ban"

TOKEN_VAL="$(grep -E '^CRYPTOAGENT_WEB_TOKEN=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
if [[ -z "$TOKEN_VAL" ]]; then
  echo "警告：$ENV_FILE 里没有找到 CRYPTOAGENT_WEB_TOKEN。"
  echo "      绑定 0.0.0.0 时程序会拒绝启动，请先执行："
  echo "      echo \"CRYPTOAGENT_WEB_TOKEN=\$(openssl rand -hex 24)\" >> $ENV_FILE"
fi
cat <<EOF

部署完成。unit 默认监听 0.0.0.0:8000，两种访问方式任选：

A. 公网直连（手机 / 任意浏览器都能开，需先在轻量控制台放行 8000 端口）
   http://<VPS_IP>:8000/?token=${TOKEN_VAL}

B. SSH 隧道（不放行端口，只在自己电脑上用，不需要服务器有桌面）
   ssh -N -L 8000:127.0.0.1:8000 ${OPS_USER}@<VPS_IP>
   然后浏览器打开 http://127.0.0.1:8000/?token=${TOKEN_VAL}

令牌就是密码：链接发出去等于控制台送出去。绑定非回环地址时，代码在缺少
CRYPTOAGENT_WEB_TOKEN 的情况下会直接拒绝启动，不会出现裸奔控制台。

注意：${SERVICE_USER} 是 nologin 服务账号，只能 sudo -u 运行程序，不能用来 SSH。

常用命令：
  systemctl status ${SERVICE_NAME}      # 看状态（旧名 cryptoagent 仍可用）
  journalctl -u ${SERVICE_NAME} -f      # 看日志
  systemctl restart ${SERVICE_NAME}     # 改完 .env 重启
  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python -m cryptoagent netcheck
  sudo -u ${SERVICE_USER} ${APP_DIR}/.venv/bin/python -m cryptoagent llmtest

EOF
