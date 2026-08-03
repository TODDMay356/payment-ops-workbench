# 跨境支付运营工作台

本机运行的 Flask 小服务，把"网页工具 + 飞书"串成一条线：

1. **CORS 拦截** —— 浏览器直连 `open.feishu.cn` 会被拦，所有飞书调用经本服务中转。
2. **凭据集中** —— AppSecret / token 只存在 `config.json`，网页端一个凭据都不存。
   工具页启动时向工作台要配置，不需要你手动填、也不需要先点什么"一键配置"。
3. **Token 自动续期** —— 后台线程续期，失败会退避重试，首页能直接看到线程是否还活着。
4. **任务在页面里跑** —— 出单监控不再弹独立 CMD 窗口，日志实时显示在首页。

---

## 一次性安装

双击 `install.bat`：创建 `venv\`、装依赖、把 `config.example.json` 复制成 `config.json`。

## 填配置

记事本打开 `config.json`：

| 字段 | 从哪来 |
|---|---|
| `feishu.app_id` / `app_secret` | https://open.feishu.cn/app 建"企业自建应用"。权限勾 `bitable:app:readonly`、`bitable:app:write`（要发私信再加 `im:message:send_as_bot`） |
| `feishu.webhook.team` | 团队群 → 设置 → 群机器人 → 自定义机器人。勾"签名校验"会给你 secret |
| `feishu.webhook.personal` | 同上，建一个只有你和机器人的群。两个填成同一个 URL 也行，工作台会去重 |
| `feishu.bitable` | 多维表格 URL `https://xxx.feishu.cn/base/APP_TOKEN?table=TABLE_ID` 里的两段 |
| `order_monitor.dir` | 出单监控脚本所在目录 |
| `order_monitor.mode` | `both` = 一次运行同时发群通知和 BD 私聊（默认）；`group` / `dm` = 只发一边 |

多维表格要先建好这些列（列名一字不差）：
`日期 / 周期 / 来源 / 指标 / 所属环节 / 层级 / 本期值 / 上期值 / 环比 / 是否异常 / 角色`

> `config.json` 已在 `.gitignore` 里。**不要**把真实 AppSecret 写进 `config.example.json` 或提交到任何仓库。

---

## 日常使用（3 步）

1. 双击桌面快捷方式 —— 工作台启动 + 浏览器打开
2. 首页「今天要做的」：
   - **转化率**：点"打开并上传 Excel" → 选文件 → 解析完自动写多维表格 + 推异常到群
   - **出单监控**：选两份报表 + 日期 + 密码 → "开始运行" → 日志就在页面里滚，跑完显示退出码
3. 首页「今天同步了什么」确认推送结果；有异常再点底部进商户成功率深挖

首页顶部那条状态栏**全绿时只占一行**，不用管它；出问题会自己变色展开，把原因写在上面。

---

## 路由表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页 |
| GET | `/conversion` `/merchant` | 两个工具页 |
| GET | `/api/health` | 探活 |
| GET | `/api/status` | 配置总览 + token 线程活性 + 出单监控脚本是否就位 |
| GET | `/api/workbench/bootstrap` | 下发中转地址给工具页（工具页据此自配置） |
| GET | `/api/activity` | 最近的同步流水（首页「今天同步了什么」） |
| POST | `/api/feishu/webhook` | 中转 webhook；可传 `_targets:["team"]` 只发指定机器人 |
| POST | `/api/feishu/bitable` | 中转多维表格写入（自动加 Bearer、自动按 500 条分批） |
| POST | `/api/feishu/open-apis/bitable/v1/apps/<app>/tables/<tbl>/records/batch_create` | 兼容飞书原生 URL 格式 |
| POST | `/api/feishu/notify-me` | App 身份发私信（需 `my_open_id`） |
| POST | `/api/run/order-monitor` | 跑出单监控，返回 `job_id` |
| GET | `/api/jobs/<id>` | 任务快照（断线重连用） |
| GET | `/api/jobs/<id>/stream` | 任务日志 SSE 实时流 |

`/api/*` 一律返回 JSON，包括出错时 —— 前端不会再把参数校验错误显示成"网络错误"。

---

## 出单监控

### 装法

把 `scripts/出单监控_workbench.py` 复制到 `order_monitor.dir` 指向的目录
（和 `出单监控.py`、`出单监控_BD私聊版.py` 放一起），**覆盖旧的同名文件**。
另外两个脚本**不用动**。

### 一次运行发两边

旧版 wrapper 走的是「出单监控.py」的 `main()`，那条路只发群通知，BD 私聊要另开脚本再跑一遍
（等于把两份 Excel 重算两次）。「出单监控_BD私聊版.py」的 `run_pipeline()` 本来就是超集，
新 wrapper 直接复用它的函数：**一次分析 → 群通知 + 多维表格 + BD 私聊**，每步各自 try/except 互不影响。

`config.json` 的 `order_monitor.mode`：`both`（默认）/ `group` / `dm`。

### AppSecret 只填一处

工作台把 `feishu.app_secret` 经环境变量 `WB_FEISHU_APP_SECRET` 传给脚本，
脚本优先用它，没有才退回「出单监控.py」里硬编码的值。
**换密钥时改 `config.json` 就够，不用再去改脚本代码。**
（走环境变量而不是命令行参数：命令行在任务管理器里是明文可见的。）

### CLI 约定

```
python 出单监控_workbench.py \
  --today <xlsx> --yesterday <xlsx> --password <pw> \
  --date YYYY-MM-DD --mode both [--app-secret xxx]
```

- `--date` 是**必填**的业务日期。旧版从文件名里抠 `YYYY-MM-DD`，而工作台把上传文件存成
  `today.xlsx` / `yesterday.xlsx`，抠不出来就退回"运行当天" —— 跑昨天的报表会被当成今天，
  「新审核通过」判定、开通天数、周报星期判断全错
- 进度逐行打到 stdout（工作台实时转发到网页）
- 成功退出码 0；失败非 0，原因在 stdout 最后一行
- 结尾不再 `input("按回车键关闭...")` —— 没有控制台了，stdin 是 DEVNULL，`input()` 会抛
  EOFError，把一次成功的运行显示成失败

工作台侧已处理：`PYTHONUNBUFFERED=1` / `PYTHONIOENCODING=utf-8`（否则 Windows 上管道是块缓冲，
日志会等脚本跑完才一次性吐出来）、Windows 下 `CREATE_NO_WINDOW`（不弹窗）、
没有 tkinter 的 Python 用空壳顶上（工作台这条路不开窗口）。

---

## 常见问题

**Q: 工具页还要填 Webhook 和 token 吗？**
A: 不用。工具页启动时会拉 `/api/workbench/bootstrap`，自动填好并锁定，自动同步默认开启。
把工具页单独拿出去用（离线单文件版）时这个请求会失败，它会自动退回本地 localStorage 那套。

**Q: token 显示"未生效"？**
A: 展开状态栏看「续期线程」那一格：显示"已停止"就重启工作台；显示"在跑 · 上次报错"就把鼠标停上去看完整报错，
多半是 AppID/AppSecret 不对或网络不通。

**Q: 出单监控点了没反应 / 报错？**
A: 日志直接显示在页面里。如果状态栏显示"出单监控脚本未找到"，改 `config.json` 的 `order_monitor.dir`。

**Q: 想加第三个工具？**
A: HTML 丢进 `static/`，`app.py` 加一条路由，首页复制一个 `<section class="act">` 改掉。

**Q: 能在多台电脑用吗？**
A: 当前是本机服务。要多台用就把整个目录拷过去，或把 `server.host` 改成 `0.0.0.0` 部署到内网机器。
