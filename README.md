<div align="center">

# 🔗 ParseHubBot FlyingLife 增强版

**Telegram 多平台聚合解析机器人 · FlyingLife 优先解析 · ParseHub 自动回退**

<p align="center">
  <a href="https://github.com/z-mio/Parse_Hub_Bot/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/z-mio/Parse_Hub_Bot?style=flat-square&color=5D6D7E" alt="License">
  </a>
  <a href="https://www.python.org/">
    <img src="https://img.shields.io/badge/Python-3.12+-blue?style=flat-square&logo=python&logoColor=white" alt="Python">
  </a>
  <a href="https://t.me/ParseHubot">
    <img src="https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=flat-square&logo=telegram&logoColor=white" alt="Telegram Bot">
  </a>
  <a href="https://github.com/astral-sh/uv">
    <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=flat-square" alt="uv">
  </a>
</p>

[**🤖 上游实例**](https://t.me/ParseHubot) ·
[**📚 相关项目**](https://github.com/z-mio/ParseHub) ·
[**🐛 问题反馈**](https://github.com/Zarah636/parse_hub_bot/issues)

</div>

---

> 本分支基于 [z-mio/parse_hub_bot](https://github.com/z-mio/parse_hub_bot) 开发，新增 FlyingLife 优先解析、失败回退和 ARM64/R5S 部署支持。

## ✨ 现有功能说明

### 解析与发送

- **双解析链路**：已启用且命中指定平台时，先调用 FlyingLife；解析、登录态或代理下载失败时，在上传 Telegram 之前整体回退到 ParseHub。
- **多平台解析**：支持视频、图集、图文、文章等类型，实际能力以下方平台表为准。
- **媒体处理**：自动转码不兼容格式、切割长图、分段超限视频，再上传到 Telegram。
- **文案处理**：将标题、正文和来源链接组合为 Bot 文案；较长的普通文案自动折叠，富文本文章转换为 Telegraph 页面。
- **缓存复用**：命中缓存时可复用 Telegram `file_id`，减少重复解析、下载和上传。
- **批量链接**：一条消息可识别并并发处理最多 10 个受支持的链接。
- **内联模式**：在任意聊天窗口输入 `@BotUsername <链接>` 可解析并选择媒体。
- **用户级设置**：解析模式、自动删除、平台开关等按 Telegram 账号单独保存。

### Bot 命令

| 命令 | 用途 |
|:---|:---|
| `/jx <链接>` | 解析链接，处理媒体后发送 |
| `/jxjx <链接>` | 绕过已有缓存，重新解析并发送 |
| `/raw <链接>` | 不进行媒体处理，尽量发送原始文件 |
| `/zip <链接>` | 不处理媒体，将解析结果打包为压缩包 |
| `/lang` | 选择 Bot 语言 |
| `/mode` | 设置直接发送链接时的默认解析模式 |
| `/switches` | 打开其他功能开关面板 |
| `/switch_auto_delete` | 启用或关闭自动删除原分享链接消息 |
| `/switch_platform` | 按平台启用或禁用解析 |

### FlyingLife 第一版范围

- 默认只接管 `douyin`，支持已验证的抖音单视频和图集。
- 媒体必须完整通过 FlyingLife 代理下载；不跨站跟随重定向，不直连源站 CDN。
- 单视频使用首图作为封面，不把封面重复发送为独立图片；图集保持网页返回顺序。
- 音频、多视频或无法可靠分类的结果直接回退 ParseHub。
- Telegram 用户权限仍由 Bot 原有管理机制控制，FlyingLife 客户端不维护额外白名单。

## 📦 支持平台一览

| 平台              | 视频 | 图文 |      其他       |
|:----------------|:--:|:--:|:-------------:|
| **Twitter / X** | ✅  | ✅  |     📝 文章     |
| **Instagram**   | ✅  | ✅  |               |
| **YouTube**     | ✅  |    |     🎵 音乐     |
| **Facebook**    | ✅  |    |               |
| **Threads**     | ✅  | ✅  |               |
| **Bilibili**    | ✅  |    |     📝 动态     |
| **抖音**          | ✅  | ✅  |     ☀️日常      |
| **TikTok**      | ✅  | ✅  |               |
| **微博**          | ✅  | ✅  |               |
| **小红书**         | ✅  | ✅  |               |
| **贴吧**          | ✅  | ✅  |               |
| **微信公众号**       |    | ✅  |               |
| **快手**          | ✅  |    |               |
| **酷安**          |    | ✅  |               |
| **皮皮虾**         | ✅  | ✅  |               |
| **最右**          | ✅  | ✅  |               |
| **小黑盒**         | ✅  | ✅  |               |
| **Snapchat**    | ✅  |    |               |
| **知乎**          | ✅  | ✅  | 🐶 问答, 专栏, 圈子 |

> 🔧 更多平台持续接入中...

## 🐳 Docker 部署教程

已发布的增强版镜像同时支持 `linux/amd64` 和 `linux/arm64`：

```text
ghcr.io/zarah636/parse_hub_bot:latest
```

R5S 推荐直接拉取 ARM64 镜像，不在设备上编译。仓库中的 `docker-compose.yaml` 则用于从当前源码本地构建，适合开发、调试或无法访问 GHCR 时使用。

### 1. 部署前准备

1. 在 [my.telegram.org](https://my.telegram.org/) 创建应用，获取 `API_ID` 和 `API_HASH`。
2. 在 Telegram 中通过 [@BotFather](https://t.me/BotFather) 创建 Bot，获取 `BOT_TOKEN`。
3. 安装 Docker Engine 和 Docker Compose 插件，确认以下命令正常：

```bash
docker version
docker compose version
```

R5S 必须运行 64 位 Linux：

```bash
uname -m
```

预期输出为 `aarch64`。如果输出 `armv7l`，说明当前是 32 位系统，无法使用本项目的 `linux/arm64` 镜像。

### 2. 创建目录和 `.env`

```bash
mkdir -p /opt/parse-hub-bot/data /opt/parse-hub-bot/downloads /opt/parse-hub-bot/logs
cd /opt/parse-hub-bot
```

创建 `/opt/parse-hub-bot/.env`：

```dotenv
# Telegram，必填
API_ID=12345678
API_HASH=替换为你的_API_HASH
BOT_TOKEN=替换为你的_BOT_TOKEN

# Bot 连接 Telegram 需要代理时才填
# BOT_PROXY=http://192.168.1.2:7890

# FlyingLife，可选
FLYINGLIFE_ENABLED=true
FLYINGLIFE_PLATFORMS=douyin
FLYINGLIFE_CONCURRENCY=1
```

> Docker 容器中的 `127.0.0.1` 指向容器自身。如果代理运行在 R5S 宿主机或局域网其他设备上，`BOT_PROXY` 应填写该设备对容器可达的局域网 IP，不要直接使用 `127.0.0.1`。

### 3. 登录 GHCR 并拉取镜像

如果镜像包是公开的，可直接拉取：

```bash
docker pull --platform linux/arm64 ghcr.io/zarah636/parse_hub_bot:latest
```

如果提示 `denied` 或 `unauthorized`，说明 GHCR 包尚未公开。在 GitHub 创建至少带 `read:packages` 权限的 Personal Access Token，然后使用标准输入登录，避免 Token 出现在 shell 历史中：

```bash
export GHCR_PAT='替换为_GitHub_PAT'
printf '%s' "$GHCR_PAT" | docker login ghcr.io -u Zarah636 --password-stdin
unset GHCR_PAT
docker pull --platform linux/arm64 ghcr.io/zarah636/parse_hub_bot:latest
```

AMD64 服务器将本文后续命令中的 `linux/arm64` 统一改为 `linux/amd64`。

可以在 R5S 上确认已拉取的镜像架构：

```bash
docker image inspect ghcr.io/zarah636/parse_hub_bot:latest --format '{{.Architecture}}'
```

预期输出 `arm64`。

### 4. 配置 FlyingLife 登录态

首次运行认证向导：

```bash
docker run --rm -it \
  --env-file .env \
  -e DATA_PATH=/app/data \
  -v "$PWD/data:/app/data" \
  ghcr.io/zarah636/parse_hub_bot:latest \
  python tools/flyinglife_auth.py
```

向导提供两种方式：

1. 输入 FlyingLife 邮箱和密码交互登录，密码只在当次进程内使用，不会保存。
2. 直接隐藏输入 Session ID。

验证成功后会生成 `data/config/flyinglife_auth.json`。也可以在 `.env` 中设置 `FLYINGLIFE_SESSION_ID`，此环境变量的优先级高于认证文件。

Session ID 可以随持久化 `data` 目录迁移到 R5S 或其他设备，但会话是否过期仍由 FlyingLife 服务端决定；失效后需要重新认证。

> Session ID 等价于可使用账号的明文凭据。不要把 `.env` 或 `data/config/flyinglife_auth.json` 提交到 Git、上传到网盘或发给其他人。

不使用 FlyingLife 时，将 `FLYINGLIFE_ENABLED=false` 或删除该配置，不需要执行认证向导。

### 5. 启动 Bot

```bash
docker run -d \
  --name parse-hub-bot \
  --restart unless-stopped \
  --init \
  --stop-timeout 30 \
  --platform linux/arm64 \
  --env-file .env \
  -e DATA_PATH=/app/data \
  -e DOWNLOAD_DIR=/app/downloads \
  -v "$PWD/data:/app/data" \
  -v "$PWD/downloads:/app/downloads" \
  -v "$PWD/logs:/app/logs" \
  ghcr.io/zarah636/parse_hub_bot:latest
```

Bot 通过长轮询主动连接 Telegram，不对外提供 Web 端口，因此启动时不需要 `-p` 端口映射。

检查容器状态和日志：

```bash
docker ps --filter name=parse-hub-bot
docker logs --tail 200 -f parse-hub-bot
```

日志中出现 Bot 启动完成信息后，在 Telegram 中向 Bot 发送 `/start`，再发送一个支持的链接验证解析链路。

### 6. R5S 上使用 SSD/NVMe

R5S 的 eMMC 或 TF 卡容量和写入寿命通常有限，建议将持久化目录放在挂载的 SSD/NVMe 上。假设磁盘挂载到 `/mnt/ssd`：

```bash
mkdir -p /mnt/ssd/parse-hub-bot/data
mkdir -p /mnt/ssd/parse-hub-bot/downloads
mkdir -p /mnt/ssd/parse-hub-bot/logs
```

然后把启动命令中三个宿主机路径分别替换为：

```text
/mnt/ssd/parse-hub-bot/data
/mnt/ssd/parse-hub-bot/downloads
/mnt/ssd/parse-hub-bot/logs
```

### 7. 升级、停止与备份

拉取新镜像后必须重建容器，单纯 `docker restart` 不会换成新镜像：

```bash
cd /opt/parse-hub-bot
docker pull --platform linux/arm64 ghcr.io/zarah636/parse_hub_bot:latest
docker stop parse-hub-bot
docker rm parse-hub-bot
```

然后重新执行第 5 步的 `docker run` 命令。因为 `data`、`downloads` 和 `logs` 均为宿主机挂载目录，删除容器不会删除配置和数据。

日常操作：

```bash
docker restart parse-hub-bot
docker stop parse-hub-bot
docker start parse-hub-bot
```

备份前先停止 Bot，以保证 SQLite 数据库一致：

```bash
cd /opt/parse-hub-bot
docker stop parse-hub-bot
tar -czf "parse-hub-bot-data-$(date +%F-%H%M).tar.gz" data .env
docker start parse-hub-bot
```

恢复时在相同目录停止 Bot、解压，再重新启动：

```bash
docker stop parse-hub-bot
tar -xzf parse-hub-bot-data-YYYY-MM-DD-HHMM.tar.gz
docker start parse-hub-bot
```

备份包同时包含 Bot Token 和 FlyingLife Session ID，应当作密钥材料保存。

### 8. 使用 Compose 从源码构建

当前 `docker-compose.yaml` 默认为 R5S 构建 `linux/arm64` 镜像。该方式会下载并编译依赖，耗时和内存占用明显高于直接拉取 GHCR 镜像。

```bash
git clone --branch codex/flyinglife-fallback https://github.com/Zarah636/parse_hub_bot.git
cd parse_hub_bot
cp .env.exa .env
# 编辑 .env，填写 Telegram 配置并选择是否启用 FlyingLife
mkdir -p data downloads logs
docker compose build --pull
# 仅在启用 FlyingLife 时需要执行下一行
docker compose run --rm bot python tools/flyinglife_auth.py
docker compose up -d
docker compose logs --tail 200 -f bot
```

如果是 AMD64 服务器，在 `.env` 中增加 `DOCKER_PLATFORM=linux/amd64`。源码升级：

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d --force-recreate
```

本地构建时如果因内存不足而失败，优先改用 GHCR 多架构镜像；必须本地构建时再为 R5S 配置 swap。

### 9. 源码运行（非 Docker）

需要 Python 3.12、[uv](https://github.com/astral-sh/uv)、FFmpeg 和 Deno：

```bash
cp .env.exa .env
uv sync
# 仅在启用 FlyingLife 时需要执行下一行
uv run tools/flyinglife_auth.py
uv run bot.py
```

---

## ⚙️ 配置说明

项目配置分为三部分：`.env` 中的 Bot/FlyingLife 环境变量、`data/config/platform_config.yaml` 中的平台代理与 Cookie，以及 Bot 内为每个 Telegram 账号保存的个人开关。

### 📝 Bot 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|:---|:---:|:---|:---|
| `API_ID` | 是 | 无 | Telegram API ID |
| `API_HASH` | 是 | 无 | Telegram API Hash |
| `BOT_TOKEN` | 是 | 无 | BotFather 生成的 Bot Token |
| `BOT_PROXY` | 否 | 直连 | Bot 连接 Telegram 的 HTTP/SOCKS 代理 URL |
| `DATA_PATH` | 否 | `data` | 会话、配置和默认 SQLite 数据库目录 |
| `DOWNLOAD_DIR` | 否 | `downloads` | 下载和媒体处理工作目录 |
| `DATABASE_URL` | 否 | `sqlite+aiosqlite:///data/db/database.db` | SQLAlchemy 数据库 URL |
| `CACHE_MAX_ENTRIES` | 否 | `30000` | 最大缓存条数，`0` 表示不限制 |
| `CACHE_DISABLED` | 否 | `false` | 完全禁用解析缓存 |
| `RATE_LIMIT_ENABLED` | 否 | `false` | 是否启用聊天级解析限速 |
| `RATE_LIMIT_BURST` | 否 | `5` | 统计窗口内允许的突发解析次数，`0` 不限制 |
| `RATE_LIMIT_BURST_WINDOW` | 否 | `60` | 突发请求统计窗口，单位秒 |
| `RATE_LIMIT_COOLDOWN` | 否 | `180` | 触发限速后的冷却时间，单位秒 |
| `RATE_LIMIT_THROTTLE` | 否 | `1` | 冷却期内每个限速窗口允许的请求数，`0` 为完全禁止 |
| `RATE_LIMIT_THROTTLE_WINDOW` | 否 | `5` | 冷却期限速窗口，单位秒 |
| `DEBUG` | 否 | `false` | 启用调试日志 |
| `DEBUG_SKIP_CLEANUP` | 否 | `false` | 调试时保留临时资源，生产环境不建议启用 |
| `DEMO_MODE` | 否 | `false` | 启用演示模式 |

### 🌐 平台配置

用于为各解析平台单独配置**代理**和 **Cookie**，位于 `data/config/platform_config.yaml`

> Docker 部署时，此文件中的解析/下载代理同样必须使用容器可达地址。下方 `127.0.0.1` 只适用于代理与 Bot 运行在同一网络命名空间的情况，普通容器应改用宿主机局域网 IP 或可解析的宿主机名。

```yaml
# ═══════════════════════ 全局默认代理 ═══════════════════════
# 当某平台未单独配置代理时，会使用全局默认代理
# 支持填写单个地址(字符串)或多个地址(列表，随机选取)

default_parser_proxies: http://127.0.0.1:7890        # 解析代理（单个）
default_downloader_proxies: # 下载代理（代理池）
  - http://127.0.0.1:7890
  - http://127.0.0.1:7891

# ═══════════════════════ 平台独立配置 ═══════════════════════
platforms:
  <platform_id>: # 平台 ID，见下方支持列表
    disable_parser_proxy: false          # 是否禁用解析代理（直连）
    disable_downloader_proxy: false      # 是否禁用下载代理（直连）
    parser_proxies: # 该平台专用解析代理池
      - http://proxy1:port
    downloader_proxies: # 该平台专用下载代理池
      - http://proxy2:port
    cookies: # 该平台 Cookie 列表（随机选取）
      - "cookie_string_1"
      - "cookie_string_2"
```

### 🔀 代理优先级

解析代理和下载代理各自遵循相同的优先级逻辑：

```
禁用代理 (disable_*_proxy: true)
  ↓ 未禁用
平台专用代理 (parser_proxies / downloader_proxies)
  ↓ 未配置
全局默认代理 (default_parser_proxies / default_downloader_proxies)
  ↓ 未配置
直连（不使用代理）
```

> 💡 当代理池中有多个地址时，每次请求会**随机选取**一个

### 🔑 支持的平台 ID

`<platform_id>` 必须是以下合法的平台 ID：

| 平台 ID       | 对应平台        |
|:------------|:------------|
| `twitter`   | Twitter / X |
| `instagram` | Instagram   |
| `youtube`   | YouTube     |
| `facebook`  | Facebook    |
| `threads`   | Threads     |
| `bilibili`  | 哔哩哔哩        |
| `douyin`    | 抖音          |
| `tiktok`    | TikTok      |
| `weibo`     | 微博          |
| `xhs`       | 小红书         |
| `tieba`     | 百度贴吧        |
| `wechat`    | 微信公众号       |
| `kuaishou`  | 快手          |
| `coolapk`   | 酷安          |
| `pipixia`   | 皮皮虾         |
| `zuiyou`    | 最右          |
| `xiaoheihe` | 小黑盒         |
| `snapchat`  | Snapchat    |
| `zhihu`     | 知乎          |

### 🍪 支持 Cookie 的平台

- `Twitter / X`
- `Instagram`
- `YouTube`
- `Bilibili`
- `抖音`
- `TikTok`
- `快手`
- `小红书`
- `知乎`

### 📌 配置示例

#### 示例 1：国内平台直连，海外平台走代理

```yaml
default_parser_proxies: http://127.0.0.1:7890
default_downloader_proxies: http://127.0.0.1:7890

platforms:
  bilibili:
    disable_parser_proxy: true
    disable_downloader_proxy: true
  douyin:
    disable_parser_proxy: true
    disable_downloader_proxy: true
  xhs:
    disable_parser_proxy: true
    disable_downloader_proxy: true
```

#### 示例 2：Twitter 配置 Cookie + 使用全局代理

```yaml
default_parser_proxies: http://127.0.0.1:7890
default_downloader_proxies: http://127.0.0.1:7890

platforms:
  twitter:
    cookies:
      - "auth_token=your_token_here; ct0=your_ct0_here"
```

#### 示例 3：YouTube 使用独立代理池

```yaml
platforms:
  youtube:
    parser_proxies:
      - http://proxy-us-1:8080
      - http://proxy-us-2:8080
      - http://proxy-eu-1:8080
    downloader_proxies:
      - http://proxy-us-1:8080
      - http://proxy-eu-1:8080
```

#### 示例 4：B站指定 Cookie 轮换 + 解析直连 + 下载走代理

```yaml
platforms:
  bilibili:
    disable_parser_proxy: true
    downloader_proxies:
      - http://127.0.0.1:7890
    cookies:
      - "SESSDATA=xxx; bili_jct=xxx; buvid3=xxx"
      - "SESSDATA=yyy; bili_jct=yyy; buvid3=yyy"
```

## 🚀 FlyingLife 优先解析（可选）

可将已验证的平台优先交给 `parse.flyinglife.cn`。这是一个事务式流程：只有 FlyingLife 解析成功且全部媒体都通过其代理下载完成，才会进入 Telegram 上传；中间任意一步失败都会放弃该次结果并重新使用 ParseHub。

```text
命中已配置平台
  → FlyingLife 解析
  → FlyingLife 代理下载全部媒体
  → Bot 原有文案、转码、切图、分段和上传流程

任意一步失败
  → 记录日志
  → ParseHub 重新解析、下载和上传
```

### FlyingLife 参数

| 变量 | 默认值 | 说明 |
|:---|:---|:---|
| `FLYINGLIFE_ENABLED` | `false` | 启用 FlyingLife 优先链路 |
| `FLYINGLIFE_BASE_URL` | `https://parse.flyinglife.cn` | FlyingLife 网页端基础地址 |
| `FLYINGLIFE_SESSION_ID` | 无 | 直接通过环境变量提供会话，优先于认证文件 |
| `FLYINGLIFE_PLATFORMS` | `douyin` | 允许走 FlyingLife 的平台 ID，多个值使用英文逗号分隔 |
| `FLYINGLIFE_PARSE_TIMEOUT` | `20` | 解析接口总超时，单位秒 |
| `FLYINGLIFE_DOWNLOAD_TIMEOUT` | `60` | 媒体代理下载的连接/读写超时，单位秒 |
| `FLYINGLIFE_CONCURRENCY` | `1` | FlyingLife 同时执行的解析/下载任务数，允许 `1`—`5`，私人实例建议保持 `1` |
| `FLYINGLIFE_FAILURE_THRESHOLD` | `3` | 连续多少次可用性失败后进入熔断 |
| `FLYINGLIFE_COOLDOWN` | `300` | 熔断后暂停尝试 FlyingLife 的时间，单位秒 |
| `FLYINGLIFE_MAX_MEDIA_BYTES` | `4294967296` | 单个代理媒体允许的最大字节数，默认 4 GiB |

客户端使用固定的当前 Chrome User-Agent，不做其他浏览器伪装。除非已经单独实测，不建议盲目将其他平台加入 `FLYINGLIFE_PLATFORMS`；没有列入的平台会直接使用 ParseHub。

### 验证与更换会话

直接使用 GHCR 容器部署时：

```bash
docker exec -it parse-hub-bot python tools/flyinglife_auth.py --reauth
docker restart parse-hub-bot
```

使用 Compose 部署时：

```bash
docker compose exec bot python tools/flyinglife_auth.py --reauth
docker compose restart bot
```

使用源码运行时：

```bash
uv run tools/flyinglife_auth.py --reauth
```

认证向导会先验证会话，成功后将 Session ID 保存到 `data/config/flyinglife_auth.json`。邮箱和密码不会写入该文件。

> 如果 `.env` 已设置 `FLYINGLIFE_SESSION_ID`，运行认证向导只会更新认证文件，不会改写 `.env`。此时应手动更新或删除 `.env` 中的旧 Session ID，然后重建容器，否则环境变量仍会覆盖新认证文件。

## 🧰 故障排查

### 容器启动后立即退出

```bash
docker ps -a --filter name=parse-hub-bot
docker logs --tail 300 parse-hub-bot
```

优先检查 `.env` 中的 `API_ID`、`API_HASH` 和 `BOT_TOKEN` 是否完整，是否把注释、空格或引号误写进值中。

### 镜像提示 `exec format error`

镜像架构与宿主机不匹配。R5S 重新拉取 `linux/arm64`：

```bash
docker pull --platform linux/arm64 ghcr.io/zarah636/parse_hub_bot:latest
```

拉取完成后按“升级、停止与备份”一节重建容器，运行中的旧容器不会自动切换镜像。

### GHCR 提示 `unauthorized` 或 `denied`

先按 Docker 教程第 3 步使用带 `read:packages` 权限的 Token 登录。依然失败时，在 GitHub 包设置中检查当前账号是否有该容器包的读取权限。

### FlyingLife 总是回退 ParseHub

先查看相关日志：

```bash
docker logs parse-hub-bot 2>&1 | grep -E 'FlyingLife|fallback'
```

常见原因包括：

- `FLYINGLIFE_ENABLED` 未设为 `true`，或目标平台不在 `FLYINGLIFE_PLATFORMS` 中。
- `data` 没有正确挂载，容器内读不到 `flyinglife_auth.json`。
- Session ID 已失效，需要运行 `flyinglife_auth.py --reauth` 并重启 Bot。
- FlyingLife 连续可用性失败进入熔断，冷却期内会直接使用 ParseHub。
- 解析结果是第一版未支持的音频、多视频或媒体类型，这是预期回退。

### Bot 无法连接 Telegram

确认宿主机的代理允许局域网或 Docker 网段访问，并在 `.env` 中使用容器可达的宿主机 IP。修改 `.env` 后要删除并重建容器，仅重启不会更新已创建容器的环境变量。

### 下载失败或磁盘被占满

检查挂载路径的空间和写入权限：

```bash
df -h
du -sh /opt/parse-hub-bot/data /opt/parse-hub-bot/downloads /opt/parse-hub-bot/logs
```

正常情况下临时下载会被清理。如果启用了 `DEBUG_SKIP_CLEANUP=true`，会刻意保留临时文件，生产部署应关闭该选项。

## 🌟 上游 Star History

<a href="https://www.star-history.com/?type=date&repos=z-mio%2FParse_Hub_Bot">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=z-mio/Parse_Hub_Bot&type=date&theme=dark&legend=top-left&sealed_token=n_B6V73FCZt16MtUaTQowR-ZQ1pdhKCd94W-9symYgpKxNI0h62EyiVFeaTIVana0l0ZYCGLFye8lCdeaXM4OPmIByiQqnbBewQtQM3bRlPd61GHsqtyg7LQGCdZoGEitbc2y_m7V9cO-04CnJwKTd7Rrct1JSNi0oLZlHPJ-DhBMpwTEp25929J4KLM" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=z-mio/Parse_Hub_Bot&type=date&legend=top-left&sealed_token=n_B6V73FCZt16MtUaTQowR-ZQ1pdhKCd94W-9symYgpKxNI0h62EyiVFeaTIVana0l0ZYCGLFye8lCdeaXM4OPmIByiQqnbBewQtQM3bRlPd61GHsqtyg7LQGCdZoGEitbc2y_m7V9cO-04CnJwKTd7Rrct1JSNi0oLZlHPJ-DhBMpwTEp25929J4KLM" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=z-mio/Parse_Hub_Bot&type=date&legend=top-left&sealed_token=n_B6V73FCZt16MtUaTQowR-ZQ1pdhKCd94W-9symYgpKxNI0h62EyiVFeaTIVana0l0ZYCGLFye8lCdeaXM4OPmIByiQqnbBewQtQM3bRlPd61GHsqtyg7LQGCdZoGEitbc2y_m7V9cO-04CnJwKTd7Rrct1JSNi0oLZlHPJ-DhBMpwTEp25929J4KLM" />
 </picture>
</a>

## 🤝 参与贡献

欢迎提交 Pull Request 或 Issue!

- 核心解析相关请前往 [ParseHub](https://github.com/z-mio/ParseHub)
- Bug 反馈请附上相关 URL 和日志信息

### 开发规范

提交代码前请至少执行:

```bash
ruff format && ruff check --fix && uv run mypy
```

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 协议开源

---

<div align="center">

**如果这个项目对你有帮助，欢迎点个 ⭐ Star!**

</div>

