# Tuite TG

一个把 RSSHub、X/Twitter List、Telegram 报警和 GraphQL ID 自动修复整合在一起的网页后台。

## 设计目标

- 用 RSSHub 把 X/Twitter List 转成 RSS。
- 支持多个 token 实例，每个实例可绑定独立 RSSHub 地址和代理。
- watcher 全局按秒轮询，适合 10 个 token 轮流检查。
- RSSHub 抓取失败时第一时间发送 Telegram 报警。
- 报警后自动使用 `tui-tg` 的思路扫描 X 前端 JS，发现 `ListLatestTweetsTimeline` 的 GraphQL query id。
- 自动用新 query id 进行 fallback 抓取测试。
- fallback 成功或失败后继续发送 Telegram 结果通知。

## Ubuntu 一键安装

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
```

如果服务器还没有 Docker，请先安装 Docker：

```bash
curl -fsSL https://get.docker.com | sudo sh
```

下载并运行中文安装向导：

```bash
cd /opt
sudo git clone https://github.com/mmk58860-code/tuite-tg.git
sudo chown -R $USER:$USER /opt/tuite-tg
cd /opt/tuite-tg
chmod +x scripts/install.sh
./scripts/install.sh
```

安装向导会要求输入：

- 网页访问端口
- 后台登录账号
- 后台登录密码

Telegram、全局轮询秒数、失败冷却分钟等配置可以启动后在网页后台修改。

安装完成后打开：

```text
http://服务器IP:你输入的端口
```

常用命令：

```bash
docker compose ps
docker compose logs -f tuite-tg
docker compose down
```

重置后台账号密码：

```bash
chmod +x scripts/reset-admin.sh
./scripts/reset-admin.sh
```

> 注意：第一次启动时，后台账号密码会写入 `data/tuite_tg.db`。如果后面只改 `.env` 里的 `WEB_PASSWORD`，不会自动修改已有数据库里的登录密码。需要重置时可以停止服务并删除数据库后重新运行安装向导。

```bash
docker compose down
rm -f data/tuite_tg.db
./scripts/install.sh
```

## Windows 开发运行

```powershell
cd C:\Users\Administrator\Desktop\tuite-tg
.\scripts\run-dev.ps1
```

## RSSHub 实例建议

推荐每个 token 一个 RSSHub 容器，全部在网页后台 `Token配置 -> RSSHub 容器` 里新增、编辑、删除：

```text
rsshub1 -> http://rsshub1:1200 -> token1 + proxy1
rsshub2 -> http://rsshub2:1200 -> token2 + proxy2
...
rsshub10 -> http://rsshub10:1200 -> token10 + proxy10
```

`docker-compose.yml` 只保留主程序，不再固定写死 `rsshub1`、`rsshub2`。这样重启服务后，不会把网页里删除或改名的 RSSHub 容器重新拉回来。
RSSHub 当前文档里 Twitter List 路由标注需要 `TWITTER_AUTH_TOKEN` 和 `TWITTER_THIRD_PARTY_API`，所以这些值也在网页新增/编辑 RSSHub 时填写。

后台里新增 token 实例时：

```text
名称：token-1
RSSHub 地址：http://rsshub1:1200
auth_token：同 RSSHub 里配置的 token
ct0：用于 fallback 抓取，建议填写
代理：fallback 抓取时使用，例如 socks5://127.0.0.1:1080
```

## GraphQL ID 变更时的处理

当 RSSHub 返回 HTTP 错误、RSS 解析失败或其它抓取异常时：

1. Tuite TG 立即发送 TG 报警。
2. 自动访问 X 前端 JS，查找 `ListLatestTweetsTimeline` 的 query id。
3. 使用发现的 query id、当前 token 的 `auth_token` 和 `ct0` 尝试 fallback 抓取。
4. 成功：保存 query id，切换该 token 到 fallback 抓取，并发送 TG 成功通知。
5. 失败：记录错误，token 进入冷却，并发送 TG 失败通知。

注意：RSSHub 官方路由不一定支持外部手动注入 query id，所以这里的“修复”是主程序 fallback 兜底，不是修改 RSSHub 内部代码。

## 预判风险

- X/Twitter 不只会改 GraphQL ID，也可能改返回 JSON 结构、feature flags、认证要求。
- 没有 `ct0` 时 fallback 基本不可用。
- 5 秒是全局轮询，不建议每个 token 都 5 秒抓一次。
- RSSHub 的 `CACHE_EXPIRE` 如果太大，watcher 会反复拿缓存；建议 30 秒起步。
- 代理质量会直接影响 token 稳定性。
- Token 和 Telegram 凭据保存在本地 SQLite，后台不要裸露公网。

## 下一步可增强

- RSSHub 健康检查和镜像版本提示。
- Telegram 按失败类型分级报警。
- Redis 去重，方便多进程部署。
- 把 token/代理加密存储。
