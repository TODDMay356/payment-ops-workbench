"""
mailbox.py - 从邮箱取报表附件，按业务日期存档

每天两封信，发件人固定，主题里带业务日期：
    FlyPay支付转化率统计表20260802      → 转化率，日期无横线
    新商户审核耗时报表2026-08-02        → 出单监控，日期有横线

以前是人工下载再上传。这里把「取信 → 认出是哪份报表 → 认出是哪天的 → 存档」自动化。

# 为什么要存档而不是用完即弃

出单监控的 CLI 是 `--today <D的报表> --yesterday <D-1的报表> --date D`，
脚本内部拿两天快照做对比分类。可一天只来一封信 —— 想跑 08-02，手里还得有 08-01 那封。
所以附件按业务日期落到 data/inbox/<kind>/<日期>.<ext> 长期留着，缺哪天一目了然。

# provider 为什么可插拔

生产环境是 Windows + Outlook 经典桌面版，走 COM 读本地已登录的配置文件
（不存密码、不注册 OAuth、不受微软关停 IMAP 基本认证影响）。
但 COM 只有 Windows 上有，开发/验证环境跑不了。所以抽了一层：

    outlook  真取信，Windows 专用
    folder   扫一个本地目录，把文件名当主题用

两个 provider 吐出同一个 Message 结构，后面的匹配 / 存档 / 索引 / 状态全部共用一条路径。
于是「认日期、防重复、缺 D-1 怎么办」这些真正容易错的逻辑，在哪儿都能验。

# 配置

单独放 mailbox.json（不进 config.json，也在 .gitignore 里）。
照着 mailbox.example.json 填。文件不存在 = 功能关闭，工作台照常启动。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "mailbox.json"
EXAMPLE_PATH = HERE / "mailbox.example.json"
DATA_DIR = HERE / "data" / "inbox"
STATE_PATH = DATA_DIR / "state.json"

# 允许的附件扩展名。报表两种格式都出现过（pandas / SheetJS 都按内容魔数认格式），
# 但别接受任意扩展名 —— 存档目录是要喂给子进程和浏览器的。
ALLOWED_EXT = {".xlsx", ".xls", ".xlsm", ".csv"}

# 没匹配上时，每个文件夹最多显示几条主题原文给人看
SAMPLE_SUBJECTS = 5

# Outlook COM 不是线程安全的，工作台是多线程 Flask（定时线程 + 请求线程都可能进来）。
# 全部串行化，简单可靠 —— 取信本来就是低频操作。
_com_lock = threading.Lock()


class MailboxError(RuntimeError):
    """配置错误或取信失败。调用方负责转成人话给用户看。"""


# ---------------------------------------------------------------- 配置

DEFAULT_RULES = [
    {"kind": "conversion", "subject_contains": "支付转化率统计表",
     "date_regex": r"(\d{8})", "date_format": "%Y%m%d"},
    {"kind": "order_monitor", "subject_contains": "新商户审核耗时报表",
     "date_regex": r"(\d{4}-\d{2}-\d{2})", "date_format": "%Y-%m-%d"},
]

KIND_LABEL = {"conversion": "转化率", "order_monitor": "出单监控"}


def _with_defaults(cfg: dict) -> dict:
    """
    补默认值。**每条返回路径都要过这里，包括「文件不存在」那条。**

    原来只有「读成功」那条补默认值，于是没建 mailbox.json 时 rules 是空的，
    而 match() 是 `for rule in cfg.get("rules") or []` —— 一条规则都没有，
    对任何邮件都返回 None。偏偏 /api/inbox/probe 的设计用法就是
    「建配置之前先跑它看该填什么」，结果它在最需要它的时候必然报 0 封匹配，
    而且和「文件夹找错了」「关键词对不上」长得一模一样，没法区分。

    注意：**不碰 enabled**。补默认值只是让诊断能跑，不等于把功能打开 ——
    fetch() 和 automation.start() 仍然靠 enabled 拦着。
    """
    cfg.setdefault("rules", DEFAULT_RULES)
    cfg.setdefault("senders", [])
    cfg.setdefault("provider", "outlook")
    cfg.setdefault("outlook", {})
    cfg.setdefault("retention_days", 30)
    return cfg


def load_config() -> dict:
    """
    读 mailbox.json。不存在就返回 enabled=False —— 没配邮箱不该让工作台起不来。
    """
    if not CONFIG_PATH.exists():
        # 说清楚是 .json 不是 .py —— 两个名字只差扩展名，实际被认错过：
        # mailbox.py 是代码（跟着仓库走），mailbox.json 是配置（要自己建，里面有密码，
        # 所以在 .gitignore 里、仓库里不会有）。
        return _with_defaults({
            "enabled": False,
            "_reason": "还没建配置文件 mailbox.json（注意是 .json，不是代码文件 mailbox.py）"
                       "—— 把 mailbox.example.json 复制一份、改名成 mailbox.json 即可",
        })
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _with_defaults({"enabled": False, "_reason": f"mailbox.json 解析失败：{e}"})
    if not isinstance(cfg, dict):
        return _with_defaults({"enabled": False, "_reason": "mailbox.json 顶层必须是一个对象"})
    cfg["_rules_from_file"] = isinstance(cfg.get("rules"), list)
    return _with_defaults(cfg)


def rules_source(cfg: dict) -> str:
    """规则是自己配的还是内置的。probe 页面要显示这个 —— 用户得知道现在在拿什么匹配。"""
    return "mailbox.json" if cfg.get("_rules_from_file") else "内置默认"


# ---------------------------------------------------------------- 状态

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)   # 原子替换，中途断电不会留下半个 JSON


def _state_section(state: dict, name: str) -> dict:
    sec = state.get(name)
    if not isinstance(sec, dict):
        sec = {}
        state[name] = sec
    return sec


def mark_run(date: str, ok: bool, detail: str = "", job_id: str = ""):
    """记一次出单监控的自动运行结果，防止同一天反复跑。"""
    state = _load_state()
    _state_section(state, "runs")[date] = {
        "ok": bool(ok), "detail": detail, "job_id": job_id, "ts": time.time(),
    }
    _save_state(state)


def run_record(date: str) -> dict | None:
    return _state_section(_load_state(), "runs").get(date)


def mark_analyzed(kind: str, date: str):
    """转化率页解析成功后回报，首页的待办卡片据此消掉。"""
    state = _load_state()
    _state_section(state, "analyzed")[f"{kind}|{date}"] = time.time()
    _save_state(state)


def is_analyzed(kind: str, date: str) -> bool:
    return f"{kind}|{date}" in _state_section(_load_state(), "analyzed")


# ---------------------------------------------------------------- 消息

class Message:
    """两个 provider 的统一产物。attachments 惰性取 —— 附件可能有几 MB，匹配不上就别读。"""

    def __init__(self, uid: str, subject: str, received_at: float,
                 sender: str = "", load_attachments=None):
        self.uid = uid
        self.subject = subject or ""
        self.received_at = received_at
        self.sender = sender or ""
        self._load = load_attachments

    def attachments(self) -> list[tuple[str, bytes]]:
        return self._load() if self._load else []


# ---------------------------------------------------------------- 匹配

def _sender_ok(msg: Message, cfg: dict) -> bool:
    """
    发件人白名单留空 = 不校验，只按主题判。

    Exchange 发来的信 SenderEmailAddress 可能是 /O=EXCHANGELABS/... 这种 X.500 DN，
    根本不含 @ —— 所以这里做子串匹配而不是相等，并且 provider 侧已经尽力换算过 SMTP。
    """
    allow = [s.strip().lower() for s in (cfg.get("senders") or []) if s.strip()]
    if not allow:
        return True
    got = (msg.sender or "").lower()
    return any(a in got for a in allow)


def match(msg: Message, cfg: dict) -> tuple[str, str] | None:
    """
    主题 → (kind, 业务日期 YYYY-MM-DD)。认不出返回 None。

    先看关键词是哪一类报表，再从主题里抠日期。抠不出来**不猜**：
    宁可报错让人看见，也不要退回"当天"—— 旧版出单监控 wrapper 就是这么把
    昨天的报表当成今天跑的，「新审核通过」判定、开通天数、周报星期判断全错。
    """
    subj = msg.subject or ""
    for rule in cfg.get("rules") or []:
        kw = rule.get("subject_contains") or ""
        if kw and kw not in subj:
            continue
        m = re.search(rule.get("date_regex") or r"(\d{4}-\d{2}-\d{2})", subj)
        if not m:
            raise MailboxError(f"主题匹配到「{rule.get('kind')}」但抠不出日期：{subj}")
        try:
            d = datetime.strptime(m.group(1), rule.get("date_format") or "%Y-%m-%d")
        except ValueError as e:
            raise MailboxError(f"主题里的日期解析失败（{m.group(1)}）：{e}")
        return rule.get("kind") or "", d.strftime("%Y-%m-%d")
    return None


def _pick_attachment(msg: Message, rule_regex: str | None) -> tuple[str, bytes] | None:
    """一封信里挑出那份报表。多个附件时取第一个扩展名合法且匹配正则的。"""
    pat = re.compile(rule_regex, re.I) if rule_regex else None
    for name, blob in msg.attachments():
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            continue
        if pat and not pat.search(name):
            continue
        return name, blob
    return None


def _rule_for(cfg: dict, kind: str) -> dict:
    for r in cfg.get("rules") or []:
        if r.get("kind") == kind:
            return r
    return {}


# ---------------------------------------------------------------- provider: folder

def _iter_folder(cfg: dict):
    """
    扫一个本地目录，把**文件名**当主题用。

    存在的意义有两个：一是 Linux 上验证整条链路（COM 只有 Windows 有）；
    二是 Outlook 那条路万一走不通时，用户可以手动把附件另存到一个目录，
    自动化的其余部分照常工作。
    """
    d = ((cfg.get("folder") or {}).get("dir") or "").strip()
    if not d:
        raise MailboxError("provider=folder 但没配 folder.dir")
    root = Path(d)
    if not root.is_dir():
        raise MailboxError(f"folder.dir 不是一个目录：{root}")

    cutoff = time.time() - int((cfg.get("outlook") or {}).get("lookback_days", 10)) * 86400
    for p in sorted(root.iterdir(), key=lambda x: x.name, reverse=True):
        if not p.is_file() or p.suffix.lower() not in ALLOWED_EXT:
            continue
        st = p.stat()
        if st.st_mtime < cutoff:
            continue
        yield Message(
            uid=f"folder:{p.name}",
            subject=p.stem,                       # 文件名即主题
            received_at=st.st_mtime,
            sender=(cfg.get("senders") or [""])[0],
            load_attachments=lambda p=p: [(p.name, p.read_bytes())],
        )


# ---------------------------------------------------------------- provider: outlook

OL_FOLDER_INBOX = 6
# PR_SMTP_ADDRESS —— 把 Exchange 的 X.500 DN 换算成真正的邮箱地址
PR_SMTP_ADDRESS = "http://schemas.microsoft.com/mapi/proptag/0x39FE001E"


def _com():
    """载入 COM 依赖。缺了就抛人话，不要让 ImportError 冒到 Flask 上去。"""
    try:
        import pythoncom            # noqa
        import win32com.client      # noqa
        return pythoncom, win32com.client
    except ImportError as e:
        raise MailboxError(
            "没装 pywin32，Outlook 取信用不了。在工作台的 venv 里执行："
            "venv\\Scripts\\python.exe -m pip install pywin32"
            f"（原始错误：{e}）"
        )


def _namespace(win32):
    try:
        return win32.Dispatch("Outlook.Application").GetNamespace("MAPI")
    except Exception as e:
        raise MailboxError(
            "连不上 Outlook。请确认装的是 Outlook **经典桌面版**并且已经登录 —— "
            "Windows 自带的「新版 Outlook」不提供 COM 接口，用不了这条路。"
            f"（原始错误：{e}）"
        )


def _walk_folders(folder, prefix="", depth=0, max_depth=4):
    """产出 (folder, 完整路径, 深度)。路径就是要填进 outlook.folder 的那个串。"""
    path = f"{prefix}/{folder.Name}" if prefix else folder.Name
    yield folder, path, depth
    if depth >= max_depth:
        return
    try:
        subs = folder.Folders
    except Exception:
        return
    for i in range(1, subs.Count + 1):
        try:
            sub = subs.Item(i)
        except Exception:
            continue
        yield from _walk_folders(sub, path, depth + 1, max_depth)


def resolve_folder(ns, path: str):
    """
    "收件箱/报表" → Outlook 文件夹对象。留空 = 默认收件箱。

    首段是收件箱时从 GetDefaultFolder(6) 起步（那是本地化名称，不同语言版本不一样，
    所以不能靠名字去 ns.Folders 里找）；否则把首段当邮箱存储名在 ns.Folders 里找。
    """
    parts = [p.strip() for p in (path or "").replace("\\", "/").split("/") if p.strip()]
    if not parts:
        return ns.GetDefaultFolder(OL_FOLDER_INBOX)

    inbox = ns.GetDefaultFolder(OL_FOLDER_INBOX)
    head = parts[0]
    if head.lower() in ("收件箱", "inbox") or head == getattr(inbox, "Name", ""):
        cur, rest = inbox, parts[1:]
    else:
        cur, rest = None, parts[1:]
        for i in range(1, ns.Folders.Count + 1):
            f = ns.Folders.Item(i)
            if getattr(f, "Name", "") == head:
                cur = f
                break
        if cur is None:
            names = [ns.Folders.Item(i).Name for i in range(1, ns.Folders.Count + 1)]
            raise MailboxError(
                f"找不到文件夹「{head}」。当前可见的顶层是：{'、'.join(names)}。"
                f"用 /api/inbox/probe 看完整的文件夹树。")

    for name in rest:
        nxt = None
        for i in range(1, cur.Folders.Count + 1):
            f = cur.Folders.Item(i)
            if getattr(f, "Name", "") == name:
                nxt = f
                break
        if nxt is None:
            have = [cur.Folders.Item(i).Name for i in range(1, cur.Folders.Count + 1)]
            raise MailboxError(
                f"「{cur.Name}」下面没有子文件夹「{name}」。它下面有：{'、'.join(have) or '（空）'}。")
        cur = nxt
    return cur


def _sender_smtp(item) -> str:
    """
    尽力拿到发件人的 SMTP 地址。

    SenderEmailAddress 对 Exchange 内部发件人返回的是 /O=EXCHANGELABS/OU=.../CN=...
    这种 X.500 DN，拿它去比对 data@inflyway.com 永远不会命中。
    按 PR_SMTP_ADDRESS → SenderEmailAddress → SenderName 的顺序退让，
    最后那个至少还能匹配显示名（"飞来汇DI"）。
    """
    try:
        v = item.PropertyAccessor.GetProperty(PR_SMTP_ADDRESS)
        if v:
            return str(v)
    except Exception:
        pass
    for attr in ("SenderEmailAddress", "SenderName"):
        try:
            v = getattr(item, attr, "")
            if v:
                return str(v)
        except Exception:
            continue
    return ""


def _received_ts(item) -> float:
    try:
        return item.ReceivedTime.timestamp()
    except Exception:
        return 0.0


def _iter_outlook(cfg: dict):
    pythoncom, win32 = _com()
    ocfg = cfg.get("outlook") or {}
    lookback = int(ocfg.get("lookback_days", 10))
    max_scan = int(ocfg.get("max_scan", 300))
    cutoff = time.time() - lookback * 86400

    pythoncom.CoInitialize()
    try:
        ns = _namespace(win32)
        folder = resolve_folder(ns, ocfg.get("folder") or "")
        items = folder.Items
        # 不用 Items.Restrict("[ReceivedTime] >= '...'")：那个日期串要按 Windows
        # 的区域设置格式化，中文环境下格式一错就静默返回空集，查起来极其费劲。
        # 倒序遍历 + 早于 cutoff 就 break，效果一样而且没有本地化陷阱。
        items.Sort("[ReceivedTime]", True)
        n = 0
        for i in range(1, items.Count + 1):
            if n >= max_scan:
                break
            n += 1
            try:
                item = items.Item(i)
            except Exception:
                continue
            if getattr(item, "Class", 43) != 43:      # olMail
                continue
            ts = _received_ts(item)
            if ts and ts < cutoff:
                break                                  # 已经排好序，后面只会更旧
            try:
                entry_id = str(item.EntryID)
            except Exception:
                entry_id = f"noid:{getattr(item, 'Subject', '')}:{ts}"

            def _load(it=item):
                out = []
                atts = it.Attachments
                for k in range(1, atts.Count + 1):
                    a = atts.Item(k)
                    name = str(a.FileName)
                    if Path(name).suffix.lower() not in ALLOWED_EXT:
                        continue
                    # COM 的 SaveAsFile 只能落盘，没有直接拿 bytes 的稳妥接口
                    tmp = DATA_DIR / "_tmp"
                    tmp.mkdir(parents=True, exist_ok=True)
                    p = tmp / name
                    a.SaveAsFile(str(p))
                    try:
                        out.append((name, p.read_bytes()))
                    finally:
                        try:
                            p.unlink()
                        except Exception:
                            pass
                return out

            yield Message(uid=f"ol:{entry_id}", subject=str(getattr(item, "Subject", "")),
                          received_at=ts, sender=_sender_smtp(item), load_attachments=_load)
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def iter_messages(cfg: dict):
    provider = (cfg.get("provider") or "outlook").lower()
    if provider == "folder":
        yield from _iter_folder(cfg)
    elif provider == "outlook":
        yield from _iter_outlook(cfg)
    else:
        raise MailboxError(f"不认识的 provider：{provider}（只支持 outlook / folder）")


# ---------------------------------------------------------------- 存档

def archive_path(kind: str, date: str, ext: str) -> Path:
    return DATA_DIR / kind / f"{date}{ext}"


def find_archived(kind: str, date: str) -> Path | None:
    d = DATA_DIR / kind
    if not d.is_dir():
        return None
    for ext in (".xlsx", ".xls", ".xlsm", ".csv"):
        p = d / f"{date}{ext}"
        if p.exists():
            return p
    return None


def archived_dates(kind: str) -> list[str]:
    d = DATA_DIR / kind
    if not d.is_dir():
        return []
    out = set()
    for p in d.iterdir():
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem):
            out.add(p.stem)
    return sorted(out, reverse=True)


def fetch(cfg: dict) -> dict:
    """
    取一轮信：遍历 → 匹配 → 存档 → 记 state。

    返回 {"saved":[{kind,date,filename,bytes}], "skipped":n, "errors":[...], "scanned":n}
    errors 里放的是「认出来了但处理失败」的，不是致命错 —— 一封坏信不该让整轮取消。
    """
    if not cfg.get("enabled"):
        raise MailboxError(cfg.get("_reason") or "邮箱自动化未启用（mailbox.json 的 enabled 是 false）")

    state = _load_state()
    seen = _state_section(state, "seen")
    saved, errors = [], []
    skipped = scanned = 0

    with _com_lock:
        for msg in iter_messages(cfg):
            scanned += 1
            try:
                hit = match(msg, cfg)
            except MailboxError as e:
                errors.append(str(e))
                continue
            if not hit:
                continue
            kind, date = hit
            if not _sender_ok(msg, cfg):
                errors.append(f"主题对得上但发件人不在白名单，已跳过：{msg.subject}（发件人 {msg.sender}）")
                continue
            if msg.uid in seen and find_archived(kind, date):
                skipped += 1
                continue

            att = _pick_attachment(msg, _rule_for(cfg, kind).get("attachment_regex"))
            if not att:
                errors.append(f"「{msg.subject}」没有可用的报表附件（只收 {'/'.join(sorted(ALLOWED_EXT))}）")
                continue

            name, blob = att
            # 保留原始扩展名：pandas 和 SheetJS 都按内容魔数认格式，
            # 但别造出「名叫 .xlsx 实际是 .xls」这种自相矛盾的文件，排查时会误导人。
            dest = archive_path(kind, date, Path(name).suffix.lower())
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(blob)
            tmp.replace(dest)

            seen[msg.uid] = {"kind": kind, "date": date, "subject": msg.subject, "ts": time.time()}
            saved.append({"kind": kind, "date": date, "filename": name, "bytes": len(blob)})

    state["last_fetch"] = {
        "ts": time.time(), "saved": len(saved), "skipped": skipped,
        "scanned": scanned, "errors": errors[:5],
    }
    _save_state(state)
    purge(cfg)
    return {"ok": True, "saved": saved, "skipped": skipped, "scanned": scanned, "errors": errors}


def purge(cfg: dict):
    """按 retention_days 清老存档。清不掉不算错 —— 文件被占用是常事。"""
    days = int(cfg.get("retention_days") or 0)
    if days <= 0:
        return
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    for kind in KIND_LABEL:
        d = DATA_DIR / kind
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.is_file() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem) and p.stem < cutoff:
                try:
                    p.unlink()
                except Exception:
                    pass
    shutil.rmtree(DATA_DIR / "_tmp", ignore_errors=True)


# ---------------------------------------------------------------- 索引

def prev_day(date: str) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def latest(cfg: dict) -> dict:
    """
    首页卡片和转化率页共用的一份现状。

    出单监控这块要回答的是「能不能自动跑」，所以除了最新日期，
    还要说清楚 D-1 在不在 —— 缺了就得让人一眼看到，而不是静默不跑。
    """
    state = _load_state()
    out = {
        "ok": True,
        "enabled": bool(cfg.get("enabled")),
        "provider": cfg.get("provider"),
        "reason": cfg.get("_reason") or "",
        "last_fetch": state.get("last_fetch"),
    }

    cdates = archived_dates("conversion")
    if cdates:
        d = cdates[0]
        out["conversion"] = {
            "date": d,
            "url": f"/api/inbox/file/conversion/{d}",
            "analyzed": is_analyzed("conversion", d),
        }
    else:
        out["conversion"] = None

    odates = archived_dates("order_monitor")
    if odates:
        d = odates[0]
        prev = prev_day(d)
        ready = find_archived("order_monitor", prev) is not None
        out["order_monitor"] = {
            "date": d,
            "prev_date": prev,
            "ready": ready,
            "reason": "" if ready else f"缺 {prev} 的报表，需要两天才能做对比",
            "run": run_record(d),
            # 每天单独一条。以前只回最新那天，于是周一回来面对周六+周日两份存档时，
            # 卡片只给得出周日的按钮，周六只能退回下面的手动表单重选文件。
            # 每天的 run 记录本来就是按日期存的（run_record 就是按 date 取的），
            # ready 也各算各的 —— 缺的信息只是没往外送而已。
            #
            # 取 31 天不是 7 天：原来那个 have 卡在 7，春节能连放 8 天，
            # 第 8 天会被静默截掉 —— 界面上根本不显示，人不会知道少跑了一天。
            # 一个月足够覆盖任何长假，每条就四个字段，多带几天不值得省。
            # （原来还有个 have 字段，全仓没有一处读它，一并删掉。）
            "days": [
                {
                    "date": x,
                    "prev_date": prev_day(x),
                    "ready": find_archived("order_monitor", prev_day(x)) is not None,
                    "run": run_record(x),
                }
                for x in odates[:31]
            ],
        }
    else:
        out["order_monitor"] = None
    return out


# ---------------------------------------------------------------- 诊断

def _rule_digest(cfg: dict) -> list[dict]:
    """规则的人话摘要，给 probe 页面显示。用户要拿它和自己的邮件主题对照。"""
    return [{"kind": r.get("kind") or "", "keyword": r.get("subject_contains") or "",
             "date_regex": r.get("date_regex") or "", "date_format": r.get("date_format") or ""}
            for r in (cfg.get("rules") or [])]


def probe(cfg: dict) -> dict:
    """
    「我该往 outlook.folder 里填什么」的答案。

    列出文件夹树，并对每个文件夹统计近 N 天里有几封信能被 rules 匹配上。
    只读，不存档、不改 state —— 可以放心反复点。
    """
    if (cfg.get("provider") or "outlook").lower() == "folder":
        d = ((cfg.get("folder") or {}).get("dir") or "").strip()
        names = sorted(p.name for p in Path(d).iterdir()) if d and Path(d).is_dir() else []
        hits = recent = near = 0
        near_msg = ""
        samples = []
        for m in _iter_folder(cfg):
            recent += 1
            if len(samples) < SAMPLE_SUBJECTS:
                samples.append((m.subject or "")[:120])
            try:
                if match(m, cfg):
                    hits += 1
            except MailboxError as e:
                near += 1
                near_msg = near_msg or str(e)
        # folders 的形状和 outlook 那边保持一致，好让同一个页面渲染两种 provider
        return {"ok": True, "provider": "folder", "dir": d, "files": names[:50],
                "matched": hits, "inbox_name": d,
                "rules_source": rules_source(cfg), "rules": _rule_digest(cfg),
                "lookback_days": int((cfg.get("outlook") or {}).get("lookback_days", 10)),
                "folders": [{"path": d, "name": d, "depth": 0,
                             "recent": recent, "matched": hits,
                             "near": near, "near_msg": near_msg,
                             "samples": samples if not hits else []}]}

    pythoncom, win32 = _com()
    lookback = int((cfg.get("outlook") or {}).get("lookback_days", 10))
    cutoff = time.time() - lookback * 86400
    tree = []
    with _com_lock:
        pythoncom.CoInitialize()
        try:
            ns = _namespace(win32)
            try:
                inbox_name = ns.GetDefaultFolder(OL_FOLDER_INBOX).Name
            except Exception:
                inbox_name = "?"
            for i in range(1, ns.Folders.Count + 1):
                store = ns.Folders.Item(i)
                for folder, path, depth in _walk_folders(store):
                    matched = recent = near = 0
                    near_msg = ""
                    samples = []
                    try:
                        items = folder.Items
                        items.Sort("[ReceivedTime]", True)
                        for k in range(1, min(items.Count, 120) + 1):
                            it = items.Item(k)
                            if getattr(it, "Class", 43) != 43:
                                continue
                            ts = _received_ts(it)
                            if ts and ts < cutoff:
                                break
                            recent += 1
                            subj = str(getattr(it, "Subject", ""))
                            if len(samples) < SAMPLE_SUBJECTS:
                                samples.append(subj[:120])
                            m = Message(uid="", subject=subj,
                                        received_at=ts, sender=_sender_smtp(it))
                            try:
                                if match(m, cfg):
                                    matched += 1
                            except MailboxError as e:
                                # match() 只在一种情况下抛：关键词命中了，但日期抠不出来。
                                # 那是「规则差一点就对了」，和「关键词根本没命中」是相反的两件事，
                                # 修法也相反。原来这里一律 pass，两种病显示成同一个 0。
                                near += 1
                                near_msg = near_msg or str(e)
                    except Exception:
                        pass
                    tree.append({"path": path, "name": folder.Name, "depth": depth,
                                 "recent": recent, "matched": matched,
                                 "near": near, "near_msg": near_msg,
                                 # 主题原文。没匹配上时，答案通常就明晃晃写在这儿 ——
                                 # 让人自己看，比让人把 JSON 贴给别人猜关键词快得多。
                                 "samples": samples if not matched else []})
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    return {
        "ok": True, "provider": "outlook", "inbox_name": inbox_name,
        "lookback_days": lookback,
        "rules_source": rules_source(cfg), "rules": _rule_digest(cfg),
        "hint": "把 matched > 0 的那个文件夹的完整路径填进 mailbox.json 的 outlook.folder，"
                "层级之间用 / 分隔（例如 收件箱/报表）。",
        "folders": tree,
    }
