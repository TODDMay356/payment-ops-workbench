"""
automation.py - 定时收邮件 + 自动跑出单监控

mailbox.py 只管「取信存档」，这里管「什么时候取」和「取到之后做什么」。
分开是为了 mailbox.py 不依赖 flask / jobs，能单独跑、单独测。

# 两件事的分工

    出单监控   全自动。取到 D 和 D-1 两份报表就直接跑，群通知 + BD 私聊照发。
              （用户明确要求：不能等人手动触发）
    转化率     只存档。解析在浏览器里做，而且用户要「首页出待办卡片，点了才打开」。
              这里一个字都不推给飞书。

# 为什么不用 APScheduler

工作台已经有一套后台线程的写法（feishu.py 的 token 续期），照抄即可，
少一个依赖就少一处装不上的可能。

# 为什么不 wait(到下次触发的秒数)

这是个开机就跑的桌面场景，笔记本合盖休眠是常态。threading.Event.wait() 在休眠期间
是不推进的，睡一晚上再打开，一个「还有 3 小时触发」的 wait 会变成「还有 3 小时触发」。
所以按**绝对时间点**算，每次最多睡 300 秒就重新看一眼钟。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import mailbox

# 一次 wait 最长睡多久。够短，休眠唤醒后能很快追上；够长，空转开销可以忽略。
TICK = 300
# 启动后隔多久跑第一次。等 Flask 端口起来，也避开开机时的磁盘争抢。
START_DELAY = 5


class Automation:
    def __init__(self, launcher=None, recorder=None, logger=None):
        # launcher / recorder 由 app.py 注入，避免 automation ← app 的循环 import
        self.launcher = launcher            # (today, yesterday, password, date) -> {"job_id":..}
        self.record = recorder or (lambda *a, **k: None)
        self.log = logger or (lambda *a, **k: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()       # 定时线程和手动「立即收取」不能同时进
        self._last: dict | None = None
        self._next_at: float | None = None
        self._running = False

    # ---------- 配置 ----------

    @property
    def cfg(self) -> dict:
        # 每次现读：改完 mailbox.json 不用重启工作台也能生效（config.json 做不到这点，
        # 它在 app.py 模块级只读一次）。取信是低频操作，读个小文件不值得缓存。
        return mailbox.load_config()

    # ---------- 线程 ----------

    def start(self):
        cfg = self.cfg
        if not cfg.get("enabled"):
            self.log(f"[automation] 未启用：{cfg.get('_reason') or 'mailbox.json 里 enabled=false'}")
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="mail-automation", daemon=True)
        self._thread.start()
        self.log(f"[automation] 已启动：provider={cfg.get('provider')} "
                 f"定时 {(cfg.get('schedule') or {}).get('at', '10:00')}")

    def stop(self):
        self._stop.set()

    def _loop(self):
        cfg = self.cfg
        on_start = bool((cfg.get("schedule") or {}).get("on_start", True))
        if on_start:
            if self._stop.wait(START_DELAY):
                return
            self._tick("启动")

        self._next_at = self._next_fire()
        while not self._stop.is_set():
            now = time.time()
            if self._next_at is None or now >= self._next_at:
                self._tick("定时")
                # 从「现在」往后算，而不是在上一个点上加一天 —— 休眠错过的那次不补跑
                self._next_at = self._next_fire()
                continue
            if self._stop.wait(min(TICK, max(1.0, self._next_at - now))):
                return

    def _next_fire(self) -> float:
        """下一个触发时刻。每天一次，含周末（报表周末也发）。"""
        at = str((self.cfg.get("schedule") or {}).get("at") or "10:00")
        try:
            hh, mm = (int(x) for x in at.split(":", 1))
        except Exception:
            hh, mm = 10, 0
        now = datetime.now()
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return t.timestamp()

    # ---------- 一轮 ----------

    def run_once(self, trigger="手动") -> dict:
        """手动「立即收取」也走这里，和定时是同一条路径。"""
        return self._tick(trigger)

    def _tick(self, trigger: str) -> dict:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "msg": "上一轮收取还没结束"}
        self._running = True
        try:
            cfg = self.cfg
            out = {"trigger": trigger, "ts": time.time()}
            try:
                res = mailbox.fetch(cfg)
            except mailbox.MailboxError as e:
                self.log(f"[automation] 收取失败：{e}")
                self.record("inbox", False, f"收取失败：{e}")
                out.update({"ok": False, "msg": str(e)})
                self._last = out
                return out
            except Exception as e:
                self.log(f"[automation] 收取异常：{e}")
                self.record("inbox", False, f"收取异常：{e}")
                out.update({"ok": False, "msg": str(e)})
                self._last = out
                return out

            out.update({"ok": True, "fetch": res})
            self._describe(res)
            out["order_monitor"] = self._maybe_run_order_monitor(cfg)
            self._last = out
            return out
        finally:
            self._running = False
            self._lock.release()

    def _describe(self, res: dict):
        """把一轮结果写成流水里那一行人话。"""
        saved = res.get("saved") or []
        if saved:
            parts = [f"{mailbox.KIND_LABEL.get(s['kind'], s['kind'])} {s['date']}" for s in saved]
            self.record("inbox", True, f"取到 {len(saved)} 份附件 · " + " / ".join(parts))
        elif res.get("errors"):
            self.record("inbox", False, res["errors"][0])
        else:
            # 没有新信不是错。但也别一声不吭 —— 用户点了「立即收取」总得有个回应。
            self.record("inbox", True, f"没有新报表（扫了 {res.get('scanned', 0)} 封，"
                                       f"跳过 {res.get('skipped', 0)} 封已处理的）")

    # ---------- 出单监控 ----------

    def _maybe_run_order_monitor(self, cfg: dict) -> dict:
        om = cfg.get("order_monitor") or {}
        if not om.get("auto_run", True):
            return {"skipped": "auto_run=false"}
        if not self.launcher:
            return {"skipped": "没有注入 launcher"}

        dates = mailbox.archived_dates("order_monitor")
        if not dates:
            return {"skipped": "还没有出单监控报表"}
        date = dates[0]

        prev = mailbox.run_record(date)
        if prev and prev.get("ok"):
            return {"skipped": f"{date} 已经跑过了"}

        today = mailbox.find_archived("order_monitor", date)
        yday_date = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        yday = mailbox.find_archived("order_monitor", yday_date)
        if not yday:
            # 脚本要两天快照做对比，缺一天是真的跑不了。说清楚缺哪天，别静默跳过。
            msg = f"出单监控 {date} 缺 {yday_date} 的报表，无法自动跑（可在首页手动上传两份）"
            self.log("[automation] " + msg)
            self.record("inbox", False, msg)
            return {"ready": False, "date": date, "missing": yday_date}

        password = om.get("password") or ""
        if not password:
            msg = "出单监控自动运行缺密码：在 mailbox.json 的 order_monitor.password 里填上"
            self.log("[automation] " + msg)
            self.record("inbox", False, msg)
            return {"ready": True, "date": date, "error": "缺密码"}

        try:
            # source="auto" 有两个作用：流水里加「自动 ·」前缀，以及跑完后写进 state.runs。
            # 少了它，「今天已经跑过了」这道闸就永远不生效 —— 每次收取都会重跑一遍，
            # 群通知和 BD 私聊跟着重发。
            r = self.launcher(str(today), str(yday), password, date, source="auto")
        except Exception as e:
            self.log(f"[automation] 起出单监控失败：{e}")
            self.record("order-monitor", False, f"{date} · 自动运行未能启动：{e}")
            mailbox.mark_run(date, False, f"未能启动：{e}")
            return {"ready": True, "date": date, "error": str(e)}

        self.log(f"[automation] 出单监控已自动启动 date={date} job={r.get('job_id')}")
        return {"ready": True, "date": date, "started": True, **r}

    # ---------- 状态 ----------

    def status(self) -> dict:
        cfg = self.cfg
        return {
            "enabled": bool(cfg.get("enabled")),
            "reason": cfg.get("_reason") or "",
            "provider": cfg.get("provider"),
            "at": (cfg.get("schedule") or {}).get("at") or "10:00",
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "running": self._running,
            "next_at": self._next_at,
            "last": self._last,
        }


_auto: Automation | None = None


def init(launcher=None, recorder=None, logger=None) -> Automation:
    global _auto
    _auto = Automation(launcher=launcher, recorder=recorder, logger=logger)
    _auto.start()
    return _auto


def get() -> Automation:
    if _auto is None:
        raise RuntimeError("automation 未初始化")
    return _auto


def stop():
    if _auto is not None:
        _auto.stop()
