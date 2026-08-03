"""
feishu.py - 飞书 API 客户端

职责：
  1. 启动时 + 后台定时刷新 tenant_access_token（基于 app_id + app_secret）
  2. webhook 中转：接收 HTML 发的消息体，分别转发到配置的群机器人和"个人告警群"机器人
  3. bitable 中转：自动加 Bearer token，写入多维表格
  4. (可选) send_dm：用 App 身份给指定 open_id 发私信

设计原则：
  - 不在 HTML 里存任何 token / 凭据
  - HTML 只关心"要发什么消息 / 写什么记录"
  - 所有鉴权 / 签名 / 路由都由本服务在 server-side 完成
"""
from __future__ import annotations
import os
import json
import time
import base64
import hashlib
import hmac
import threading
from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin

import requests


FEISHU_BASE = "https://open.feishu.cn"
TOKEN_URL = f"{FEISHU_BASE}/open-apis/auth/v3/tenant_access_token/internal"
BITABLE_BATCH_CREATE = (
    "/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create"
)
DM_SEND_URL = "/open-apis/im/v1/messages"
# 飞书 bitable batch_create 单次记录数上限
BITABLE_BATCH_LIMIT = 500


class FeishuError(RuntimeError):
    """飞书 API 错误。code != 0 或 HTTP 失败都会抛。"""


class FeishuClient:
    """单例风格：工作台进程内只需要一个 client。"""

    def __init__(self, config: dict, logger=None):
        self.cfg = (config or {}).get("feishu") or {}
        self.log = logger or (lambda *a, **k: None)
        self._token: Optional[str] = None
        self._expire_at: float = 0.0
        self._last_refresh_at: float = 0.0
        self._last_error: Optional[str] = None
        # RLock 而非 Lock：get_token() 是持锁调用 _refresh_token_blocking() 的，
        # 而后者自己也要拿锁。用普通 Lock 时，只要走到「token 缺失/过期」这条路
        # （启动时拉 token 失败、或后台线程挂了），请求线程就会永久死锁。
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        creds = self.cfg.get("app_id") and self.cfg.get("app_secret")
        if creds:
            self._bootstrap_token()

    # ---------- token 管理 ----------

    def _bootstrap_token(self):
        """启动时同步拉一次 token；失败不致命，等后台再试。"""
        try:
            self._refresh_token_blocking()
        except Exception as e:
            self.log(f"[feishu] 启动时拉 token 失败：{e}，稍后后台重试")

    def start_background_refresh(self):
        """启动后台线程，每 1.5h 刷新一次 token。"""
        if not (self.cfg.get("app_id") and self.cfg.get("app_secret")):
            self.log("[feishu] 未配置 app_id/app_secret，跳过后台刷新")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._refresh_loop, name="feishu-token-refresh", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    # 正常间隔 90 分钟（飞书默认 7200s = 2h 过期，留余量）
    REFRESH_INTERVAL = 90 * 60
    # 失败后的退避序列：一次网络抖动不该让 token 静默失效
    RETRY_BACKOFF = (30, 60, 120, 300)

    def _refresh_loop(self):
        """
        成功就等 90 分钟；失败就按 30s→60s→120s→300s 退避重试。
        原来的写法是失败后照样再睡 90 分钟，一次抖动就可能撞上 2h 过期，
        中间那段时间所有 bitable 写入都会 401，而且没人知道。
        """
        delay = self.REFRESH_INTERVAL
        attempt = 0
        while not self._stop.wait(delay):
            try:
                self._refresh_token_blocking()
                delay = self.REFRESH_INTERVAL
                attempt = 0
            except Exception as e:
                self.log(f"[feishu] 后台刷 token 失败：{e}")
                delay = self.RETRY_BACKOFF[min(attempt, len(self.RETRY_BACKOFF) - 1)]
                attempt += 1

    def _refresh_token_blocking(self):
        payload = {
            "app_id": self.cfg["app_id"],
            "app_secret": self.cfg["app_secret"],
        }
        try:
            r = requests.post(TOKEN_URL, json=payload, timeout=10)
            r.raise_for_status()
            data = r.json()
            if data.get("code") not in (0, "0"):
                raise FeishuError(f"拉 token 失败：{data}")
        except Exception as e:
            with self._lock:
                self._last_error = str(e)
            raise
        with self._lock:
            self._token = data["tenant_access_token"]
            self._expire_at = time.time() + int(data.get("expire", 7200)) - 300
            self._last_refresh_at = time.time()
            self._last_error = None
        self.log("[feishu] tenant_access_token 已刷新")

    def get_token(self) -> str:
        """取一个当前有效的 token；临期会自动刷一次。"""
        with self._lock:
            if not self._token:
                self._refresh_token_blocking()
                return self._token
            if time.time() >= self._expire_at:
                self._refresh_token_blocking()
            return self._token

    def token_status(self) -> dict:
        """首页状态条用。thread_alive 是为了能直接回答『后台续期线程还在跑吗』。"""
        with self._lock:
            return {
                "configured": bool(self.cfg.get("app_id") and self.cfg.get("app_secret")),
                "valid": bool(self._token) and time.time() < self._expire_at,
                "refresh_at": self._expire_at,
                "seconds_left": max(0, int(self._expire_at - time.time())),
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "last_refresh_at": self._last_refresh_at or None,
                "last_error": self._last_error,
            }

    # ---------- webhook 签名（飞书自定义机器人 v2） ----------

    @staticmethod
    def _webhook_sign(secret: str, ts: int) -> str:
        """飞书自定义机器人签名：HMAC-SHA256(secret, f'{ts}\n{secret}')，base64。"""
        h = hmac.new(secret.encode("utf-8"), f"{ts}\n{secret}".encode("utf-8"), hashlib.sha256)
        return base64.b64encode(h.digest()).decode("utf-8")

    def _post_webhook(self, url: str, body: dict, secret: str = "") -> dict:
        """签名 + POST 到指定 webhook。secret 由调用方传入（它已经拿着配置项了）。"""
        body = dict(body)  # 拷贝，避免污染原始
        body.pop("timestamp", None)
        body.pop("sign", None)
        if secret:
            ts = int(time.time())
            body["timestamp"] = str(ts)
            body["sign"] = self._webhook_sign(secret, ts)
        r = requests.post(url, json=body, timeout=10)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return {"status": r.status_code, "data": data}

    def broadcast_webhook(self, body: dict, targets: list[str] | None = None) -> dict:
        """
        把消息体转发到已配置的 webhook。
        同一 URL 只发一次（team 和 personal 配成同 URL 时不会重复推送）。

        targets: 只发给指定的配置名，如 ["team"] 或 ["personal"]。
                 出单监控靠这个把『群通知』和『BD 私聊』分别路由到不同机器人。
                 None = 全发（转化率日报走这条）。
        """
        results = {}
        seen_urls = set()
        wh_cfg = self.cfg.get("webhook") or {}
        for name, wh in wh_cfg.items():
            if targets is not None and name not in targets:
                continue
            if not wh or not wh.get("url"):
                continue
            url = wh["url"]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                results[name] = self._post_webhook(url, body, wh.get("secret") or "")
            except Exception as e:
                results[name] = {"error": str(e)}
        return results

    # ---------- 多维表格 ----------

    @staticmethod
    def _parse_field_for_bitable(fields: dict) -> dict:
        """
        HTML 端发来的字段都是文本字符串，里面混了百分比 / 布尔等。
        多维表格需要正确的类型才能做筛选 / 聚合分析。
        这里按字段名约定做转换。
        """
        out = dict(fields)
        # 日期: "2026-07-31" → 毫秒时间戳（飞书短日期字段要求）
        v = fields.get("日期")
        if isinstance(v, str) and v.strip():
            try:
                dt = datetime.strptime(v.strip(), "%Y-%m-%d")
                out["日期"] = int(dt.timestamp() * 1000)
            except ValueError:
                pass
        # 百分比字段: "82.35%" → 0.8235（飞书百分比字段存小数形式）
        for k in ("本期值", "上期值"):
            v = fields.get(k)
            if isinstance(v, str) and v.endswith("%"):
                try:
                    out[k] = round(float(v.replace("%", "")) / 100, 6)
                except ValueError:
                    pass
        v = fields.get("环比")
        if isinstance(v, str) and v.endswith("%"):
            try:
                out["环比"] = round(float(v.replace("%", "").replace("+", "")) / 100, 6)
            except ValueError:
                pass
        v = fields.get("是否异常")
        if v == "是":
            out["是否异常"] = True
        elif v == "否":
            out["是否异常"] = False
        return out

    def bitable_batch_create(self, records: list[dict], app_token: str = None, table_id: str = None) -> dict:
        """
        records: list of {fields: {...}} 或 [{"fields": {...}}, ...]
        自动转换百分比/布尔为多维表格可分析的数字/复选框类型。
        """
        app_token = app_token or self.cfg.get("bitable", {}).get("app_token")
        table_id = table_id or self.cfg.get("bitable", {}).get("table_id")
        if not (app_token and table_id):
            raise FeishuError("bitable 缺少 app_token / table_id")

        # 统一转成 {fields: {...}} 格式
        parsed = []
        for r in records:
            f = r.get("fields") if isinstance(r, dict) and "fields" in r else r
            parsed.append({"fields": self._parse_field_for_bitable(f)})

        url = FEISHU_BASE + BITABLE_BATCH_CREATE.format(app_token=app_token, table_id=table_id)
        token = self.get_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        # 飞书 batch_create 单次上限 500 条；超了整批失败，
        # 而且返回的报错跟字段类型错误长得一样，很难查。这里直接分批。
        written = 0
        last = None
        for i in range(0, len(parsed), BITABLE_BATCH_LIMIT):
            chunk = parsed[i:i + BITABLE_BATCH_LIMIT]
            r = requests.post(url, headers=headers, json={"records": chunk}, timeout=30)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text, "http_status": r.status_code}
            last = data
            if data.get("code") not in (0, "0"):
                return {
                    "ok": False, "http_status": r.status_code, "data": data,
                    "count": written,   # 已经写进去多少条，方便判断要不要手工清理
                }
            written += len(chunk)
        return {"ok": True, "http_status": 200, "data": last, "count": written}

    def bitable_status(self) -> dict:
        b = self.cfg.get("bitable") or {}
        return {
            "configured": bool(b.get("app_token") and b.get("table_id")),
            "app_token": b.get("app_token"),
            "table_id": b.get("table_id"),
        }

    # ---------- 个人推送（可选：App 身份发 DM） ----------

    def send_dm(self, receive_id: str, content: dict, msg_type: str = "text", id_type: str = "open_id") -> dict:
        """用 App 身份给指定用户发私信。需要 App 有 im:message:send_as_bot 权限。"""
        if not receive_id:
            raise FeishuError("send_dm 缺少 receive_id")
        url = f"{FEISHU_BASE}{DM_SEND_URL}?receive_id_type={id_type}"
        token = self.get_token()
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "receive_id": receive_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
            timeout=10,
        )
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text, "http_status": r.status_code}
        return {"ok": data.get("code") in (0, "0"), "http_status": r.status_code, "data": data}

    # ---------- 配置总览（给首页用） ----------

    def config_summary(self) -> dict:
        wh = self.cfg.get("webhook") or {}
        return {
            "feishu_app_configured": bool(self.cfg.get("app_id") and self.cfg.get("app_secret")),
            "token": self.token_status(),
            "webhook_team_configured": bool(wh.get("team", {}).get("url")),
            "webhook_personal_configured": bool(wh.get("personal", {}).get("url")),
            "bitable": self.bitable_status(),
        }


# ---------- 模块级单例 ----------

_client: Optional[FeishuClient] = None
_lock = threading.Lock()


def init(config: dict, logger=None) -> FeishuClient:
    global _client
    with _lock:
        if _client is None:
            _client = FeishuClient(config, logger=logger)
            _client.start_background_refresh()
        return _client


def get() -> FeishuClient:
    if _client is None:
        raise RuntimeError("FeishuClient 尚未初始化")
    return _client
