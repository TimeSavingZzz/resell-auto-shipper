# resell-auto-shipper

二手闲置平台上卖**数字资料/虚拟商品**的卖家用的自动发货机器人：**付款后自动发货 + 成交自动补货 + Web 管理页 + 微信/邮件告警**。一次写好交付文案，之后的「发链接→标记已发货→补回库存」全部自动。可在 Windows / Linux / Docker 随处部署。

> ⚠️ 本项目只服务你自己账号的正常经营。自动化交互可能违反你所在平台的用户协议，可能触发风控、验证码或封号；**不要绕过验证码、不要批量铺货、不要滥用**。使用风险自负，作者不承担任何后果。

---

## 它能做什么

- **只读事件监听**：连接平台消息通道，收到「等待卖家发货」等事件才触发后续流程，不会自言自语、不会乱发消息。
- **自动发货**：买家付款 → 自动在会话里发出你配好的交付文案（网盘分享口令/链接等）→ 调平台发货接口把订单标记已发货（`AUTO_SHIP=1`）。
- **自动补货**：成交并交付后，把同款商品自动重新上架补到 `min_online` 份在线（`AUTO_RELIST=1`；需给商品配好 `listing` 发布模板）。
- **Web 管理页**：浏览器里增删改商品/交付文案/价格/发布模板、停用商品、更新登录凭证、看报表、手动补货/手动发货（`AUTO_SHIP` 等开关默认关闭，无配置时不会真发任何东西）。
- **微信告警**：Server酱 / PushPlus。覆盖三类——登录凭证失效/触发风控、发货/补货失败、每单成交+发货成功。凭证失效后不再傻等：**微信提醒 → 你在管理页贴新凭证 → bot 自动热恢复**。
- **邮件告警（可选第二通道）**：SMTP 配齐后，告警在微信之外同步发邮件（独立于主渠道，可单独启用）。
- **凭证定时主动检测（`app/cookiewatch`）**：与 bot 的被动检测互补，默认每 12 小时独立探测一次；业务性失效/风控才提醒，网络抖动不误报。bot 停了也兜底。
- **断线重连 / 凭证热更新 / 商品热重载**，全程无需重启进程。

## 架构

```
        ┌────────────────────────────┐
        │   你的二手闲置平台服务端     │   （登录/消息/发货/上架接口，均为私有协议）
        └─────────────┬──────────────┘
                      │  消息通道(只读, 唯一长连接)
                      ▼
        ┌──── 进程 A：bot ──────────────┐
        │  app.main                     │
        │  ├─ ListenerClient (监听/心跳)│
        │  ├─ AutoShipTrigger(发货话术) │
        │  └─ RepublishManager(自动补货)│
        └──────────▲───────────────────┘
                   │ 轮询 data/control.json（约 5s，热更新握手）
        ┌──────────┴───────────────────┐
        │  进程 B：admin (app.control)  │  Flask，短连接 HTTP
        │  写 config/products.json /    │
        │  .env 并落盘 control.json     │
        └──────────▲───────────────────┘
                   │ 浏览器(建议经 HTTPS 反代 + 登录密码)
```

数据：
- `config/products.json` — 你的商品配置（交付文案/价格/发布模板/enabled），**不入仓库**，只放示例。
- `data/relist.db` — 别名表（同款商品的多个历史 item_id 归一）+ 每次真实上架留痕。
- `data/deliveries.db` — 每单已发货记录（幂等，防重复发货）。
- `data/control.json` — 两进程热更新握手（products 版本号 + 最新凭证）。

## 目录结构

```
app/
  main.py        bot 入口（消息监听）
  client.py      ListenerClient：连接/心跳/Ack/事件派发/热更新/补货触发/告警
  trigger.py     自动发货决策器（收到待发货 → 发文案 → 标发货）
  relist.py      补货协调器 + CLI
  autoship.py    手动单发 CLI（发文案 + 标发货）
  capture.py     上架模板采集 CLI
  control.py     Web 管理页（Flask 独立进程）
  alerter.py     微信/邮件/日志告警（节流去重）
  config.py      配置读取（.env/config.json）+ 凭证热写入
  catalog.py     商品目录 + item_id→商品 别名注册表
  token_api.py / crypto.py / ws_sender.py / parser.py …  平台私有协议适配层
config/products.example.json   商品配置示例（复制成 products.json 使用）
deploy/systemd/resell-*.service/timer   Linux systemd 示例
docker-compose.yml / Dockerfile
tests/                         离线测试（不触网、不写真实数据）
```

## 快速开始（本地）

```bash
git clone https://github.com/TimeSavingZzz/resell-auto-shipper.git && cd resell-auto-shipper

python -m venv .venv
# Windows: .venv\Scripts\activate     Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                          # 填你的凭证（真实值只写本地 .env）
cp config/products.example.json config/products.json   # 改成你的真实商品
```

`.env` 关键键：

| 键 | 说明 | 默认 |
|---|---|---|
| `COOKIES_STR` | 平台网页版整串 Cookie（含会话键） | 必填 |
| `AUTO_SHIP` | 1=付款自动发货 | 0 |
| `AUTO_RELIST` | 1=成交自动补货 | 0 |
| `ADMIN_TOKEN` | 管理页访问令牌，留空则拒绝一切请求 | 空 |
| `CONTROL_HOST/PORT` | 管理页监听地址/端口 | 127.0.0.1:8788 |
| `ALERT_CHANNEL` | `log` / `serverchan` / `pushplus` | log |
| `ALERT_SENDKEY` | Server酱 SendKey | 空 |
| `ALERT_PUSHPLUS_TOKEN` | PushPlus token | 空 |
| `ALERT_THROTTLE` | 同类告警最短间隔(秒) | 600 |
| `ALERT_EMAIL_TO` | 告警邮件收件邮箱（留空则不启用邮件） | 空 |
| `ALERT_SMTP_HOST/PORT` | SMTP 服务器/端口（465=SSL，587=STARTTLS） | smtp.qq.com:465 |
| `ALERT_SMTP_USER/PASS` | SMTP 账号 / 授权码（非登录密码） | 空 |
| `HEARTBEAT_INTERVAL/TIMEOUT`、`MAX_BACKOFF` | 心跳/退避 | 15/30/60 |

> 邮件是**并列附加渠道**：SMTP 配齐后，同一告警会微信（主渠道）+ 邮件都发；即使 `ALERT_CHANNEL=log`，配了邮件也会发邮件。

`config/products.json` 两种格式都支持，建议用新格式：

```jsonc
{
  "min_online": 2,
  "products": {
    "note-pack": {                    // ← 稳定 product_key（条目键即 key）
      "key": "note-pack",
      "name": "考点速记电子版",
      "enabled": true,                // false=停用：不再自动发货/补货，报表仍统计历史
      "message": "发给买家的交付全文（网盘分享口令/链接 + 使用说明）",
      "source_item_id": "…",          // 该商品任一在线 item_id，用于把成交链接归到本商品
      "aliases": [],                  // 其它历史 item_id
      "listing": {                    // 自动补货所需的上架模板
        "title": "…", "description": "…",
        "images": ["https://example.com/img/…"],
        "price": "9.9", "delivery": "无需邮寄"
      }
    }
  }
}
```

旧的扁平格式 `{"<item_id>": {"name":…, "message":…}}` 仍兼容（条目键即商品，message 即交付文案）。

### 运行

```bash
# 1) bot：付款自动发货 + 成交自动补货
AUTO_SHIP=1 AUTO_RELIST=1 python -m app.main

# 2) 只跑一次就退（连通性自测，不重连）
XY_SINGLE_RUN=1 python -m app.main

# 3) Web 管理页（另开终端；也可放另一台机）
python -m app.control
# 浏览器打开 http://127.0.0.1:8788 ，填入 .env 里的 ADMIN_TOKEN
```

> 同一账号只跑**一个** bot 实例（重复长连接会互相顶号）。
> 管理页建议只在本机/内网访问，或经 nginx HTTPS 反代，不要开公网裸端口。

### 其它 CLI

```bash
python -m app.relist --report     # 只读报表：各商品在线/已售/补发历史
python -m app.relist --backfill   # 真实补发到 min_online（写操作）
python -m app.autoship --item <item_id> --buyer <buyer> --order <order>   # 手动单发+标发货
python -m app.cookiewatch   # 手动跑一次凭证主动检测（正常静默，失效会告警）
```

## 管理页 API

| 方法/路径 | 作用 |
|---|---|
| `GET  /api/products` | 商品列表 + 在线/已售/别名/最近补发 |
| `POST /api/products` | 新建商品 |
| `PUT  /api/products/<key>` | 更新商品（含 `enabled` 停用/启用） |
| `DELETE /api/products/<key>` | 删除商品 |
| `POST /api/cookie` | 更新登录凭证（写 .env + control.json，bot 自动热恢复） |
| `POST /api/backfill` | 手动补货到在线 N 份（真实上架） |
| `POST /api/autoship` | 手动发货：`{item_id, buyer_id, order_id}` |
| `GET  /api/report` | 汇总报表 |

另有 `resolve` / `preview` / `register` 三个端点，对应管理页里的「登记已在售商品」：
粘贴平台分享链接或纯数字 id → 解析出商品 id → 尝试抓详情自动生成上架模板 → 登记为「付款自动发货」商品。

## 部署

### systemd（Linux 单机，推荐）

```bash
sudo adduser --system reseller
sudo mkdir -p /opt/resell-shipper && sudo chown reseller:reseller /opt/resell-shipper
# clone 本仓库到 /opt/resell-shipper，python -m venv .venv，pip install -r requirements.txt，
# 放好 .env / config/products.json
sudo cp deploy/systemd/resell-bot.service deploy/systemd/resell-admin.service \
     deploy/systemd/resell-cookiewatch.service deploy/systemd/resell-cookiewatch.timer /etc/systemd/system/
# 编辑各 unit：User / WorkingDirectory / ExecStart 里的路径换成你的
sudo systemctl daemon-reload
sudo systemctl enable --now resell-bot resell-admin resell-cookiewatch.timer
journalctl -u resell-bot -f      # 看日志
```

### Docker

```bash
cp .env.example .env        # 填真实 COOKIES_STR、ADMIN_TOKEN 等
cp config/products.example.json config/products.json
docker compose up -d --build   # 起 bot + admin
```

管理页再经宿主机 nginx 反代：`proxy_pass http://127.0.0.1:8788;`，并只允许 HTTPS。

## 适配你的平台

本项目针对**作者所在的二手闲置平台**的私有协议适配好了，可直接使用（长连接签名握手见 `app/token_api.py` / `app/crypto.py`，消息收发见 `app/ws_sender.py` / `app/sender.py`，上架/发货原语见 `app/publish_api.py`，链接解析见 `app/links.py`）。换一个平台时需要自己适配这几处：

1. **登录凭证**：`COOKIES_STR` 的来源与「是否还有额外的 token 换取 / 请求签名」——看 `token_api.py`、`crypto.py`。
2. **事件消息长连接**：事件类型、字段结构、心跳与回包格式——`client.py` / `parser.py`。
3. **发消息 / 标发货 / 上架**：对应接口的地址、入参与错误码——`sender.py` / `publish_api.py`。
4. **商品链接解析**：分享链接 / 落地页 → 商品 id 的正则规则——`links.py`。

适配好的平台键字段大概率和你平台的业务字段不同，属正常，改适配层即可，核心自动发货/补货/告警流程不动。

## 登录凭证失效了怎么办

bot 握手返回 401/403、token 获取失败、或返回平台风控码时：
1. 收到「凭证失效/触发风控」告警（默认日志渠道则看日志）；
2. 重新登录平台网页版，复制整串 Cookie；
3. 管理页「运行凭证」里粘贴 → 更新凭证；
4. bot 数秒内按新凭证热重连，告警不会重复轰炸（已节流）。

## 测试

```bash
python -m pytest -q        # 离线：不触网、不写真实 .env/data
```

## 安全与合规

- **绝不绕过验证码/风控**。遇到风控码 / 令牌非法等，只告警并停止，不无限重试。
- 凭证/令牌永不进日志、不进仓库。真实 `config/products.json`（含真实图片/交付链接）不入库，只提交示例。
- 自动化出口会触发平台风控判定：控制频率、谨慎小号先行、不在生产机乱试。
- 交付文案含你的真实资料链接，注意别把 `config/products.json`、`.env`、`data/*.db` 提交进公开仓库（`.gitignore` 已默认排除）。
