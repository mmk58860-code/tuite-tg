#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

say() {
  printf '%s\n' "$1"
}

say_error() {
  printf '%s\n' "$1" >&2
}

ask() {
  local prompt="$1"
  local default_value="${2:-}"
  local value
  if [[ -n "$default_value" ]]; then
    read -r -p "$prompt [$default_value]: " value
    printf '%s' "${value:-$default_value}"
  else
    read -r -p "$prompt: " value
    printf '%s' "$value"
  fi
}

ask_required() {
  local prompt="$1"
  local default_value="${2:-}"
  local value
  while true; do
    value="$(ask "$prompt" "$default_value")"
    if [[ -n "$value" ]]; then
      printf '%s' "$value"
      return
    fi
    say_error "不能为空，请重新输入。"
  done
}

ask_password() {
  local first second
  while true; do
    read -r -s -p "请输入后台登录密码: " first
    printf '\n'
    if [[ -z "$first" ]]; then
      say_error "密码不能为空，请重新输入。"
      continue
    fi
    read -r -s -p "请再次输入后台登录密码: " second
    printf '\n'
    if [[ "$first" == "$second" ]]; then
      printf '%s' "$first"
      return
    fi
    say_error "两次密码不一致，请重新输入。"
  done
}

validate_port() {
  local port="$1"
  [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 ))
}

require_command() {
  local command_name="$1"
  local install_hint="$2"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    say "未检测到 $command_name。"
    say "$install_hint"
    exit 1
  fi
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    date +%s%N | sha256sum | awk '{print $1}'
  fi
}

write_env() {
  local env_file="$1"
  cat > "$env_file" <<EOF
WEB_PORT=$WEB_PORT
WEB_USERNAME=$WEB_USERNAME
WEB_PASSWORD=$WEB_PASSWORD
TUITE_TG_SECRET_KEY=$TUITE_TG_SECRET_KEY
GLOBAL_POLL_SECONDS=$GLOBAL_POLL_SECONDS
FAILURE_COOLDOWN_MINUTES=$FAILURE_COOLDOWN_MINUTES
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
APPRISE_URLS=$APPRISE_URLS

# RSSHub 示例容器配置。也可以启动后在网页后台添加外部 RSSHub 地址。
RSSHUB1_TWITTER_AUTH_TOKEN=
RSSHUB1_TWITTER_THIRD_PARTY_API=
RSSHUB1_PROXY_URI=
RSSHUB2_TWITTER_AUTH_TOKEN=
RSSHUB2_TWITTER_THIRD_PARTY_API=
RSSHUB2_PROXY_URI=
EOF
}

say "========================================"
say " Tuite TG Ubuntu 安装向导"
say "========================================"
say "本向导会生成 .env 配置文件，并使用 Docker Compose 启动服务。"
say ""

require_command "docker" "请先安装 Docker：curl -fsSL https://get.docker.com | sudo sh"
if ! docker compose version >/dev/null 2>&1; then
  say "未检测到 Docker Compose 插件。请先安装 docker-compose-plugin。"
  exit 1
fi

if [[ -f .env ]]; then
  say "检测到已有 .env 配置。"
  read -r -p "是否覆盖并重新配置？输入 yes 覆盖，其他输入取消: " overwrite
  if [[ "$overwrite" != "yes" ]]; then
    say "已取消安装向导，现有 .env 未修改。"
    exit 0
  fi
  cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"
  say "已备份旧配置。"
fi

while true; do
  WEB_PORT="$(ask "请输入网页访问端口" "8000")"
  if validate_port "$WEB_PORT"; then
    break
  fi
  say_error "端口号必须是 1-65535 之间的数字。"
done

WEB_USERNAME="$(ask_required "请输入后台登录账号" "admin")"
WEB_PASSWORD="$(ask_password)"
TUITE_TG_SECRET_KEY="$(generate_secret)"
GLOBAL_POLL_SECONDS="5"
FAILURE_COOLDOWN_MINUTES="10"
TELEGRAM_BOT_TOKEN=""
TELEGRAM_CHAT_ID=""
APPRISE_URLS=""

write_env ".env"

say ""
say "配置已写入 .env。"
say "正在构建并启动服务..."
docker compose up -d --build

say ""
say "安装完成。"
say "后台地址：http://服务器IP:$WEB_PORT"
say "登录账号：$WEB_USERNAME"
say "登录密码：你刚才输入的密码"
say ""
say "常用命令："
say "查看状态：docker compose ps"
say "查看日志：docker compose logs -f tuite-tg"
say "停止服务：docker compose down"
