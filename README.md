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

## 快速启动

Windows 开发运行：

```powershell
cd C:\Users\Administrator\Desktop\tuite-tg
.\scripts\run-dev.ps1
```

Docker 运行：

```bash
cp .env.example .env
docker compose up -d --build
```

默认后台：

```text
http://服务器IP:8000
账号：admin
密码：admin12345
```

上线前请修改 `.env`：

```env
WEB_USERNAME=admin
WEB_PASSWORD=换成强密码
TUITE_TG_SECRET_KEY=随机长字符串
TELEGRAM_BOT_TOKEN=你的TG机器人token
TELEGRAM_CHAT_ID=你的chat id
GLOBAL_POLL_SECONDS=5
FAILURE_COOLDOWN_MINUTES=10
```

## RSSHub 实例建议

推荐每个 token 一个 RSSHub 容器：

```text
rsshub1 -> http://127.0.0.1:1201 -> token1 + proxy1
rsshub2 -> http://127.0.0.1:1202 -> token2 + proxy2
...
rsshub10 -> http://127.0.0.1:1210 -> token10 + proxy10
```

`docker-compose.yml` 里已经放了 `rsshub1` 和 `rsshub2` 示例。复制到 `rsshub10` 后修改端口和环境变量即可。
RSSHub 当前文档里 Twitter List 路由标注需要 `TWITTER_AUTH_TOKEN` 和 `TWITTER_THIRD_PARTY_API`，所以 `.env` 里也要给每个实例补对应值。
也可以生成 10 个服务块：

```powershell
.\scripts\generate-rsshub-compose.ps1 -Count 10 -StartPort 1201
```

后台里新增 token 实例时：

```text
名称：token-1
RSSHub 地址：http://127.0.0.1:1201
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

- 一键生成 10 个 RSSHub compose 服务。
- RSSHub 健康检查和镜像版本提示。
- Telegram 按失败类型分级报警。
- Redis 去重，方便多进程部署。
- 把 token/代理加密存储。
