"""
app.py - 工作台 Flask 主程序

启动：python app.py
默认监听：http://127.0.0.1:5050

路由：
  GET  /                          工作台首页
  GET  /conversion                每日转化率工具（static/conversion.html）
  GET  /merchant                  单商户成功率工具（static/merchant.html）
  GET  /api/health                探活
  GET  /api/status                配置总览（首页状态条用）
  GET  /api/workbench/bootstrap   下发飞书中转地址给工具页（工具页据此自配置）
  POST /api/feishu/webhook        转发 webhook 消息到所有已配置机器人
  POST /api/feishu/bitable        写入多维表格（自动加 Bearer token）
  POST /api/feishu/notify-me      (可选) App 身份发 DM
  POST /api/run/order-monitor     跑出单监控，返回 job_id
  GET  /api/inbox/latest          邮箱取到的报表现状（首页待办卡片）
  POST /api/inbox/fetch           立即收取一次
  GET  /api/inbox/probe           诊断：邮箱文件夹树 + 匹配数
  GET  /api/inbox/file/<k>/<日期>  把存档报表发给工具页
  GET  /api/jobs/<id>             任务快照（断线重连用）
  GET  /api/jobs/<id>/stream      任务日志 SSE 实时流
"""
from __future__ import annotations
import sys
import json
import time
import logging
import re
import subprocess
import tempfile
import threading
from collections import deque
from pathlib import Path

from flask import (
    Flask, render_template, send_from_directory, jsonify, request, abort,
    Response, stream_with_context
)
from werkzeug.exceptions import HTTPException

import requests

import automation
import feishu
import jobs
import mailbox


HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
EXAMPLE_CONFIG_PATH = HERE / "config.example.json"
STATIC_DIR = HERE / "static"


# ---------- 配置加载 ----------

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        # 没填配置也不致命，首页会提示"未配置"
        if EXAMPLE_CONFIG_PATH.exists():
            try:
                return json.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[config] 解析 config.json 失败：{e}", file=sys.stderr)
        return {}


config = _load_config()
server_cfg = (config.get("server") or {})
HOST = server_cfg.get("host", "127.0.0.1")
PORT = int(server_cfg.get("port", 5050))
DEBUG = bool(server_cfg.get("debug", False))


# ---------- 日志 ----------

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("workbench")


# ---------- Flask app ----------

app = Flask(__name__, template_folder="templates", static_folder="static")

# 在模块级初始化飞书客户端（含后台续期线程）。
# 原来只在 main() 里 init，用 flask run / waitress 之类的方式启动时就不会执行，
# 于是所有飞书接口静默 503。
feishu.init(config, logger=log.info)


@app.after_request
def _cors(resp):
    """本机同源调用为主，这里加宽松 CORS 是给将来部署留口子（不影响本地）。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


# ---------- 同步流水 ----------
# 首页「今日结果」要能回答一个问题：今天到底推成功了没。
# 只放内存里，重启即清空 —— 这是给「今天」看的，不是账本。
_activity: deque = deque(maxlen=100)
_activity_lock = threading.Lock()


def _record(kind: str, ok: bool, detail: str):
    with _activity_lock:
        _activity.appendleft({
            "ts": time.time(),
            "kind": kind,      # webhook | bitable | order-monitor | inbox
            "ok": bool(ok),
            "detail": detail,
        })


@app.route("/api/activity")
def activity():
    with _activity_lock:
        return jsonify({"ok": True, "items": list(_activity)})


@app.errorhandler(HTTPException)
def _json_errors(e: HTTPException):
    """
    /api/* 一律返回 JSON。

    原来 abort(400, description=...) 返回的是 HTML 错误页，前端 await r.json()
    直接解析失败，于是所有的参数校验错误在界面上都显示成假的「网络错误」，
    真正的原因（没填密码、文件没选）反而看不到。
    """
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "msg": e.description, "code": e.code}), e.code
    return e


# ---------- 工作台首页 ----------

@app.route("/")
def index():
    summary = {}
    if config:
        try:
            summary = feishu.get().config_summary()
        except Exception:
            # feishu 客户端未初始化（配置缺失）也不让首页崩
            summary = {"feishu_app_configured": False}
    return render_template("index.html", summary=summary, has_config=bool(config))


# ---------- 工具页 ----------

@app.route("/conversion")
def conversion():
    if not (STATIC_DIR / "conversion.html").exists():
        abort(404, description="static/conversion.html 不存在")
    return send_from_directory(STATIC_DIR, "conversion.html")


@app.route("/merchant")
def merchant():
    if not (STATIC_DIR / "merchant.html").exists():
        abort(404, description="static/merchant.html 不存在")
    return send_from_directory(STATIC_DIR, "merchant.html")


# ---------- API ----------

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "service": "workbench",
        "feishu_ready": bool(config.get("feishu", {}).get("app_id")),
    })


@app.route("/api/status")
def status():
    if not config:
        return jsonify({
            "config_loaded": False,
            "hint": "未找到 config.json。请复制 config.example.json 为 config.json 并填写。",
        })
    try:
        s = feishu.get().config_summary()
    except Exception as e:
        return jsonify({"config_loaded": True, "error": str(e)})
    s["order_monitor"] = _order_monitor_status()
    try:
        s["mailbox"] = automation.get().status()
    except Exception as e:
        s["mailbox"] = {"enabled": False, "reason": str(e)}
    return jsonify({"config_loaded": True, "summary": s})


@app.route("/api/workbench/bootstrap")
def bootstrap():
    """
    工具页（conversion.html）启动时拉这个接口来自我配置。

    以前靠首页那个「一键配置并打开」按钮往 localStorage 里写默认值，
    但它写的键名（fsAuto / fsCardMode）和工具页读的键名（auto / cardMode）对不上，
    结果凭据填了、开关却从来没被勾上。改成由工作台下发配置后，
    就不存在「忘了点那个按钮」这回事了。

    不再下发 auto：解析完不自动同步，什么时候发飞书由人点按钮决定。
    否则一换对比日期就会同步一次 —— 去重 key 是「周期+本期日期」，换日期就是新 key。

    工具页直连（不经工作台）时这个请求会失败，它会自动退回原来的 localStorage 方案。
    """
    b = (config.get("feishu") or {}).get("bitable") or {}
    base = request.host_url.rstrip("/")
    return jsonify({
        "ok": True,
        "managed": True,
        "webhook_relay": f"{base}/api/feishu/webhook",
        "bitable_relay": f"{base}/api/feishu",
        "bitable": {
            # 工作台会忽略工具页回传的这两个值，统一用 config.json 里的。
            # 这里下发只是为了让工具页能拼出一个合法的 URL。
            "app_token": b.get("app_token") or "workbench",
            "table_id": b.get("table_id") or "workbench",
        },
        "card": True,
        # 工具页据此决定要不要显示「AI 总结」按钮、要不要还让用户填 key
        "ai": {
            "available": bool((config.get("ai") or {}).get("api_key")),
            "model": (config.get("ai") or {}).get("model") or "deepseek-chat",
            "endpoint": f"{base}/api/ai/summary",
        },
    })


@app.route("/api/ai/summary", methods=["POST"])
def ai_summary():
    """
    AI 总结代转。和飞书那套一个思路：key 只存在 config.json，
    浏览器只管把提示词发过来。

    解决两件事：
      1. 浏览器直连 api.deepseek.com 会被 CORS 拦（离线 file:// 必然失败），
         以前只能靠「复制提示词 → 粘到大模型 → 把回答粘回来」这条人工退路
      2. 以前每次都要在页面上手输 API Key
    """
    ai = config.get("ai") or {}
    key = ai.get("api_key") or ""
    if not key:
        abort(400, description="未配置 AI key。请在 config.json 的 ai.api_key 填入后重启工作台；"
                               "或继续用页面上的「复制提示词」手动跑。")

    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        abort(400, description="缺少 prompt")

    base = (ai.get("base_url") or "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": ai.get("model") or "deepseek-chat",
        "max_tokens": int(body.get("max_tokens") or ai.get("max_tokens") or 2000),
        "stream": False,
        "messages": [
            {"role": "system",
             "content": body.get("system") or "你是一名专业、谨慎、只依据给定数据说话的支付数据分析师。"},
            {"role": "user", "content": prompt},
        ],
    }
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=int(ai.get("timeout") or 120),
        )
    except Exception as e:
        _record("ai", False, f"调用失败：{e}")
        return jsonify({"ok": False, "msg": f"连接 {base} 失败：{e}"}), 502

    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text[:500]}

    content = ""
    try:
        content = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception:
        pass
    if not content:
        msg = (data.get("error") or {}).get("message") or json.dumps(data, ensure_ascii=False)[:200]
        _record("ai", False, f"返回异常：{msg}")
        return jsonify({"ok": False, "msg": f"AI 返回异常（HTTP {r.status_code}）：{msg}"}), 502

    _record("ai", True, f"{payload['model']} 生成 {len(content)} 字")
    return jsonify({"ok": True, "content": content, "model": payload["model"]})


def _client_or_503():
    try:
        return feishu.get()
    except Exception:
        abort(503, description="飞书客户端未初始化（请先填 config.json 后重启工作台）")


@app.route("/api/feishu/webhook", methods=["POST"])
def relay_webhook():
    """
    接收工具页发的消息体（msg_type / content / 可选 card），
    签名后转发到已配置的 webhook。

    可选 body._targets: ["team"] / ["personal"]，只发指定机器人。
    出单监控用它把『群通知』和『BD 私聊』分开路由；不传则全发。
    """
    body = request.get_json(silent=True) or {}
    if not body:
        abort(400, description="body 为空或不是 JSON")
    if "msg_type" not in body:
        abort(400, description="缺少 msg_type")
    targets = body.pop("_targets", None)
    if targets is not None and not isinstance(targets, list):
        abort(400, description="_targets 必须是数组，如 [\"team\"]")

    client = _client_or_503()
    results = client.broadcast_webhook(body, targets=targets)
    if not results:
        msg = "config.json 中没有配置任何 webhook（feishu.webhook.team / personal）"
        if targets:
            msg = f"没有匹配的 webhook 配置：{targets}"
        _record("webhook", False, msg)
        return jsonify({"ok": False, "msg": msg}), 400

    # 聚合：只要有一个成功就算 ok
    any_ok = any(r.get("status", 0) == 200 and (r.get("data") or {}).get("code") in (0, "0")
                 for r in results.values())
    _record("webhook", any_ok,
            ("已推送 " + "、".join(results.keys())) if any_ok else "推送失败：" + json.dumps(results, ensure_ascii=False)[:200])
    return jsonify({"ok": any_ok, "results": results})


def _do_bitable_write():
    """两个 bitable 路由共用：校验 → 写入 → 记流水。"""
    body = request.get_json(silent=True) or {}
    records = body.get("records")
    if not isinstance(records, list) or not records:
        abort(400, description="缺少 records 数组")
    # 工具页发的是 [{fields:{...}}, ...]；只发 fields 列表时也兼容
    if isinstance(records[0], dict) and "fields" not in records[0] and records[0]:
        records = [{"fields": r} for r in records]

    client = _client_or_503()
    try:
        res = client.bitable_batch_create(records)
    except feishu.FeishuError as e:
        _record("bitable", False, str(e))
        abort(400, description=str(e))

    _record("bitable", res.get("ok", False),
            f"写入 {res.get('count', 0)} 条" if res.get("ok")
            else "写入失败：" + json.dumps(res.get("data"), ensure_ascii=False)[:200])
    return jsonify(res), (200 if res.get("ok") else 502)


@app.route("/api/feishu/bitable", methods=["POST"])
def relay_bitable():
    """接收 {records:[...]} 写入多维表格，自动加 Bearer token。"""
    return _do_bitable_write()


@app.route("/api/feishu/open-apis/bitable/v1/apps/<app_token>/tables/<table_id>/records/batch_create", methods=["POST"])
def relay_bitable_feishu_style(app_token, table_id):
    """
    兼容工具页 bitableUrl() 拼出来的飞书原生 URL 格式。
    URL 里的 app_token/table_id 一律忽略，统一用 config.json 里的配置。
    """
    return _do_bitable_write()


@app.route("/api/feishu/notify-me", methods=["POST"])
def notify_me():
    """
    可选：用 App 身份给指定 open_id 发私信。
    Body: {"receive_id": "ou_xxx", "content": {"text": "..."} | md, "msg_type": "text"|"interactive"}
    需要：App 有 im:message:send_as_bot 权限。
    """
    body = request.get_json(silent=True) or {}
    receive_id = body.get("receive_id") or (config.get("feishu") or {}).get("my_open_id")
    if not receive_id:
        abort(400, description="缺少 receive_id（可填在 config.json 的 feishu.my_open_id）")
    try:
        client = feishu.get()
    except Exception:
        abort(503, description="飞书客户端未初始化")
    res = client.send_dm(
        receive_id=receive_id,
        content=body.get("content") or {"text": body.get("text", "")},
        msg_type=body.get("msg_type", "text"),
        id_type=body.get("id_type", "open_id"),
    )
    return jsonify(res), (200 if res.get("ok") else 502)


# ---------- 出单监控 ----------

def _order_monitor_paths() -> tuple[Path, Path]:
    """脚本位置从 config.json 读，缺配置时退回原来的默认路径。"""
    om = config.get("order_monitor") or {}
    d = Path(om.get("dir") or r"D:\商户数据\出单耗时监控\出单监控")
    w = d / (om.get("wrapper") or "出单监控_workbench.py")
    return d, w


def _order_monitor_python() -> str:
    """
    跑出单监控用哪个 Python。

    默认是 sys.executable，也就是工作台自己的 venv —— 但那个 venv 只装了 flask + requests，
    而出单监控脚本要 pandas / openpyxl，直接跑会 ModuleNotFoundError。
    要么把 pandas 装进工作台 venv，要么在这里指向一个已经有 pandas 的解释器。
    """
    return (config.get("order_monitor") or {}).get("python") or sys.executable


def _order_monitor_status() -> dict:
    d, w = _order_monitor_paths()
    py = _order_monitor_python()
    deps_ok, deps_msg = _check_order_monitor_deps(py)
    return {
        "dir": str(d),
        "wrapper": str(w),
        "available": w.exists() and deps_ok,
        "script_found": w.exists(),
        "python": py,
        "deps_ok": deps_ok,
        "deps_msg": deps_msg,
        "mode": (config.get("order_monitor") or {}).get("mode") or "both",
    }


_deps_cache: dict = {}

# 出单监控脚本要的三个包。注意 xlrd：pandas 是按文件内容的魔数认格式的，
# 报表如果是老的 .xls（OLE2），pandas 会去找 xlrd —— 光有 openpyxl 不够，
# openpyxl 只管 .xlsx。反过来 xlrd 2.x 也只管 .xls，两个都得有。
_OM_DEPS = ("pandas", "openpyxl", "xlrd")

_DEP_PROBE = (
    "import importlib.util, sys\n"
    "print(','.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))\n"
)


def _check_order_monitor_deps(py: str) -> tuple[bool, str]:
    """
    确认那个解释器里三个包都在，并且**说清楚少的是哪个**。
    结果缓存，别每次刷状态都起进程。
    """
    if py in _deps_cache:
        return _deps_cache[py]
    try:
        r = subprocess.run(
            [py, "-c", _DEP_PROBE, *_OM_DEPS],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            ok, msg = False, f"解释器跑不起来：{(r.stderr or '').strip()[:120]}"
        else:
            missing = [m for m in (r.stdout or "").strip().split(",") if m]
            ok = not missing
            msg = "" if ok else "缺少 " + "、".join(missing)
    except Exception as e:
        ok, msg = False, f"解释器不可用：{e}"
    _deps_cache[py] = (ok, msg)
    return ok, msg


def _launch_order_monitor(today_path: str, yesterday_path: str,
                          password: str, report_date: str, source: str = "manual") -> dict:
    """
    拼 argv 起任务。手动上传（路由）和邮箱自动化（automation）共用这一条路径，
    保证两边的依赖检查、AppSecret 下发、流水记录完全一致。

    参数不合法时抛 ValueError，由调用方决定是转成 400 还是写进流水。
    """
    om_dir, wrapper = _order_monitor_paths()
    if not wrapper.exists():
        raise ValueError(f"找不到出单监控脚本：{wrapper}。请在 config.json 的 order_monitor.dir 里指向正确目录。")

    py = _order_monitor_python()
    deps_ok, deps_msg = _check_order_monitor_deps(py)
    if not deps_ok:
        pkgs = " ".join(_OM_DEPS)
        raise ValueError(
            f"{py} {deps_msg}。装进这个解释器："
            f'"{py}" -m pip install {pkgs} '
            f"—— 或在 config.json 的 order_monitor.python 里指向一个已经装好的解释器。")

    mode = (config.get("order_monitor") or {}).get("mode") or "both"
    # AppSecret 经环境变量下发，脚本里就不用再硬编码一份。
    # 走 env 而不是命令行参数：命令行在任务管理器里是明文可见的。
    app_secret = (config.get("feishu") or {}).get("app_secret") or ""

    label = {"both": "群通知 + BD 私聊", "group": "仅群通知", "dm": "仅 BD 私聊"}.get(mode, mode)
    prefix = "自动 · " if source == "auto" else ""

    def _done(job):
        """出单监控脚本直连飞书，不走中转路由，所以流水得在这里补记。"""
        ok = job.returncode == 0
        if ok:
            detail = f"{prefix}{report_date} · {label} 已完成"
        else:
            tail = next((l for l in reversed(job.lines) if l.strip()), "")
            detail = (f"{prefix}{report_date} · 失败（退出码 {job.returncode}）"
                      f"{('：' + tail[:120]) if tail else ''}")
        _record("order-monitor", ok, detail)
        if source == "auto":
            # 只有自动跑的才记进 state —— 手动重跑不该被"今天已经跑过了"挡住
            try:
                mailbox.mark_run(report_date, ok, detail, job_id=job.id)
            except Exception:
                pass

    job_id = jobs.start_job(
        "order-monitor",
        [_order_monitor_python(), str(wrapper),
         "--today", str(today_path),
         "--yesterday", str(yesterday_path),
         "--password", password,
         "--date", report_date,
         "--mode", mode],
        cwd=str(om_dir),
        env_extra={"WB_FEISHU_APP_SECRET": app_secret} if app_secret else None,
        on_done=_done,
    )
    log.info(f"出单监控任务已启动 job={job_id} date={report_date} mode={mode} source={source}")
    return {"ok": True, "job_id": job_id, "mode": mode}


@app.route("/api/run/order-monitor", methods=["POST"])
def run_order_monitor():
    """
    起一个后台任务跑出单监控，立刻返回 job_id；
    前端拿 job_id 连 /api/jobs/<id>/stream 看实时日志。

    不再弹独立 CMD 窗口 —— 原来那套 CREATE_NEW_CONSOLE 不但弹得不稳定，
    而且起完进程就 return ok，脚本失败了工作台也一无所知。
    """
    today = request.files.get("today")
    yesterday = request.files.get("yesterday")
    password = request.form.get("password", "")
    report_date = request.form.get("date", "")

    if not today or not today.filename:
        abort(400, description="请选择今天的 Excel 报表")
    if not yesterday or not yesterday.filename:
        abort(400, description="请选择昨天的 Excel 报表")
    if not password:
        abort(400, description="请输入脚本密码")
    if not report_date:
        abort(400, description="请选择报表业务日期")

    # 存到临时目录，不再往脚本目录里扔 _wb_today_*.xlsx（那些文件从来没人清理）。
    # 保留原始扩展名：pandas 虽然是按内容魔数认格式的（所以 .xls 存成 .xlsx 也能读），
    # 但别留这种自相矛盾的文件名，出问题时会误导排查。
    def _ext(fs, default=".xlsx"):
        e = Path(fs.filename or "").suffix.lower()
        return e if e in (".xls", ".xlsx", ".xlsm") else default

    tmp = Path(tempfile.mkdtemp(prefix="wb_order_"))
    today_path = tmp / ("today" + _ext(today))
    yesterday_path = tmp / ("yesterday" + _ext(yesterday))
    today.save(str(today_path))
    yesterday.save(str(yesterday_path))

    try:
        r = _launch_order_monitor(str(today_path), str(yesterday_path), password, report_date)
    except ValueError as e:
        abort(400, description=str(e))
    return jsonify(r)


# ---------- 邮箱自动化 ----------

@app.route("/api/inbox/latest")
def inbox_latest():
    """首页待办卡片 + 转化率页自动加载都读这个。"""
    try:
        return jsonify(mailbox.latest(mailbox.load_config()))
    except mailbox.MailboxError as e:
        return jsonify({"ok": False, "enabled": False, "msg": str(e)})


@app.route("/api/inbox/fetch", methods=["POST"])
def inbox_fetch():
    """手动「立即收取」。和定时走同一条路径，包括自动跑出单监控。"""
    try:
        r = automation.get().run_once("手动")
    except RuntimeError:
        abort(503, description="邮箱自动化未初始化")
    if not r.get("ok"):
        return jsonify({"ok": False, "msg": r.get("msg") or "收取失败"}), 400
    return jsonify(r)


@app.route("/api/inbox/probe")
def inbox_probe():
    """
    诊断：列出邮箱里的文件夹树，以及每个文件夹近期能匹配上几封报表邮件。
    Windows 上第一次配置时先跑这个，再决定 outlook.folder 填什么。只读，不存档。
    """
    try:
        return jsonify(mailbox.probe(mailbox.load_config()))
    except mailbox.MailboxError as e:
        abort(400, description=str(e))
    except Exception as e:
        abort(500, description=f"探测失败：{e}")


@app.route("/api/inbox/file/<kind>/<date>")
def inbox_file(kind, date):
    """把存档的报表发给转化率页，让它不用人选文件就能解析。"""
    if kind not in mailbox.KIND_LABEL:
        abort(404, description=f"未知的报表类型：{kind}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        abort(400, description="日期格式应为 YYYY-MM-DD")
    p = mailbox.find_archived(kind, date)
    if not p:
        abort(404, description=f"存档里没有 {mailbox.KIND_LABEL[kind]} {date} 的报表")
    return send_from_directory(p.parent, p.name, as_attachment=False,
                               download_name=f"{mailbox.KIND_LABEL[kind]}_{date}{p.suffix}")


@app.route("/api/inbox/analyzed", methods=["POST"])
def inbox_analyzed():
    """转化率页解析成功后回报，首页那张待办卡片据此消掉。"""
    body = request.get_json(silent=True) or {}
    kind = body.get("kind") or "conversion"
    date = body.get("date") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        abort(400, description="缺少合法的 date（YYYY-MM-DD）")
    mailbox.mark_analyzed(kind, date)
    return jsonify({"ok": True})


# ---------- 任务日志 ----------

@app.route("/api/jobs/<job_id>")
def job_snapshot(job_id):
    since = request.args.get("since", type=int, default=0)
    snap = jobs.snapshot(job_id, since=since)
    if snap is None:
        abort(404, description="任务不存在或已被淘汰")
    return jsonify({"ok": True, **snap})


@app.route("/api/jobs/<job_id>/stream")
def job_stream(job_id):
    """SSE：逐行把脚本输出推给浏览器。"""
    if jobs.get(job_id) is None:
        abort(404, description="任务不存在或已被淘汰")
    since = request.args.get("since", type=int, default=0)

    def gen():
        for kind, payload in jobs.stream(job_id, since=since):
            if kind == "line":
                yield f"event: line\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            elif kind == "ping":
                yield ": ping\n\n"           # 注释帧，保活用，浏览器会忽略
            else:
                yield f"event: done\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 万一将来挂到 nginx 后面，别让它缓冲住
            "Connection": "keep-alive",
        },
    )


# ---------- 邮箱自动化：起定时线程 ----------
# 放在这里而不是文件开头：init() 会立刻拉起后台线程，而线程要回调 _launch_order_monitor，
# 那个函数得先定义完。和 feishu.init 一样放模块级，用 waitress / flask run 启动时也生效。
automation.init(launcher=_launch_order_monitor, recorder=_record, logger=log.info)


# ---------- 启动 ----------

def main():
    if not CONFIG_PATH.exists():
        log.warning("未找到 config.json —— 工作台会以空配置启动，所有飞书接口会返回 400/503。")
        log.warning("请复制 config.example.json 为 config.json 并填写。")
    log.info(f"工作台启动: http://{HOST}:{PORT}")
    log.info(f"首页:        http://{HOST}:{PORT}/")
    log.info(f"转化率工具:  http://{HOST}:{PORT}/conversion")
    log.info(f"成功率工具:  http://{HOST}:{PORT}/merchant")
    log.info("Ctrl+C 停止")
    try:
        app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=False, threaded=True)
    finally:
        for stopper in (lambda: feishu.get().stop(), automation.stop):
            try:
                stopper()
            except Exception:
                pass


if __name__ == "__main__":
    main()
