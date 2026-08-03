# -*- coding: utf-8 -*-
"""
出单监控 — 工作台入口

    python 出单监控_workbench.py --today T.xlsx --yesterday Y.xlsx \
        --password xxx --date 2026-08-02 --mode both

把这个文件放到 config.json 里 order_monitor.dir 指向的目录，
也就是和「出单监控.py」「出单监控_BD私聊版.py」同一个文件夹。

--mode
    both   群通知 + 多维表格 + BD 私聊（默认，一次跑完全部发）
    group  群通知 + 多维表格
    dm     多维表格 + BD 私聊

和旧版 wrapper 的区别
    旧版 monkey-patch 掉 tkinter 再调「出单监控.py」的 main()，那条路只发群通知，
    BD 私聊要另开一个脚本手动再跑一遍（等于把两份 Excel 重算两次）。
    「出单监控_BD私聊版.py」的 run_pipeline() 本来就是超集 —— 群通知、多维表格、
    BD 私聊都在里面，每一步各自 try/except 互不影响。这里直接复用它的函数，
    一次分析、按 mode 分发通知，不再需要人工触发第二个脚本。

    另外三处旧版会出问题的地方：
    1. 业务日期。run_pipeline() 用 extract_date_from_filename() 从文件名里抠
       YYYY-MM-DD，而工作台把上传的文件存成 today.xlsx / yesterday.xlsx，
       抠不出来就退回 datetime.now()。跑昨天的报表会被当成今天，
       「新审核通过」判定、开通天数、周报星期判断全错。这里改成显式传入 --date。
    2. AppSecret。原脚本硬编码在「出单监控.py」里，改密钥要改代码。
       这里优先用工作台传进来的（环境变量 WB_FEISHU_APP_SECRET），
       改一处 config.json 就够；没传才退回脚本里的硬编码值。
    3. 结尾的 input("按回车键关闭窗口...")。工作台不再开控制台，stdin 是 DEVNULL，
       input() 会直接抛 EOFError，把一次成功的运行显示成失败。已去掉。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import traceback
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BD_SCRIPT = os.path.join(SCRIPT_DIR, "出单监控_BD私聊版.py")


def log(msg=""):
    # 工作台按行读 stdout，flush 保证网页上是实时滚动而不是跑完才出现
    print(msg, flush=True)


def stub_tkinter_if_missing():
    """
    「出单监控.py」在模块顶层就 import tkinter，但工作台这条路从头到尾不开窗口。
    正常 Windows Python 自带 tk，装了精简版 / 无 GUI 的 Python 则会直接 ImportError，
    整个任务在载入阶段就挂掉。这里在载入前塞一个空壳，让 import 过得去；
    因为我们从不调用原脚本的 main()，那些对话框函数不会被用到。
    """
    try:
        import tkinter  # noqa: F401
        return False
    except ImportError:
        pass

    import types

    def _unavailable(*a, **kw):
        raise RuntimeError("工作台模式下不应调用 tkinter 对话框")

    tk = types.ModuleType("tkinter")
    tk.Tk = _unavailable
    for sub, funcs in (
        ("filedialog", ("askopenfilename", "asksaveasfilename")),
        ("simpledialog", ("askstring",)),
        ("messagebox", ("showinfo", "showerror", "showwarning")),
    ):
        m = types.ModuleType(f"tkinter.{sub}")
        for fn in funcs:
            setattr(m, fn, _unavailable)
        setattr(tk, sub, m)
        sys.modules[f"tkinter.{sub}"] = m
    sys.modules["tkinter"] = tk
    return True


def load_module(path: str, name: str):
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到脚本：{path}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--today", required=True, help="今天的报表路径")
    p.add_argument("--yesterday", required=True, help="昨天的报表路径")
    p.add_argument("--password", required=True)
    p.add_argument("--date", required=True, help="报表业务日期 YYYY-MM-DD")
    p.add_argument("--mode", default="both", choices=["both", "group", "dm"])
    p.add_argument("--app-secret", default=os.environ.get("WB_FEISHU_APP_SECRET", ""),
                   help="覆盖脚本里硬编码的 FEISHU_APP_SECRET；默认读环境变量 WB_FEISHU_APP_SECRET")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        log(f"❌ 业务日期格式不对：{args.date}（应为 YYYY-MM-DD）")
        return 2

    for label, path in (("今天", args.today), ("昨天", args.yesterday)):
        if not os.path.exists(path):
            log(f"❌ 找不到{label}的报表文件：{path}")
            return 2

    # ---- 载入两个原脚本 ----
    # 注意：original 必须由 bd.load_original_module() 产出，
    # 否则 bd 内部会再 exec 一份「出单监控.py」，我们对 AppSecret 的覆盖就落在另一个实例上。
    try:
        if stub_tkinter_if_missing():
            log("ℹ️ 当前 Python 没有 tkinter，已用空壳替代（工作台模式不需要窗口）")
        bd = load_module(BD_SCRIPT, "bd_monitor")
        original = bd.load_original_module()
    except Exception as e:
        log(f"❌ 载入脚本失败：{e}")
        traceback.print_exc()
        return 2

    # ---- 密码校验 ----
    # 直接调 run_pipeline 会绕过原脚本 main() 里的密码弹窗，这里补回来
    if args.password != getattr(original, "PASSWORD", None):
        log("❌ 密码不正确。")
        return 2

    # ---- AppSecret：工作台传进来的优先 ----
    if args.app_secret:
        original.FEISHU_APP_SECRET = args.app_secret
        log("🔑 使用工作台下发的 AppSecret")
    elif not getattr(original, "FEISHU_APP_SECRET", ""):
        log("⚠️ 出单监控.py 里的 FEISHU_APP_SECRET 是空的，且工作台没有下发 —— "
            "多维表格和 BD 私聊会失败（群通知不受影响）")

    report_date = args.date
    send_group = args.mode in ("both", "group")
    send_dm = args.mode in ("both", "dm")
    log(f"📅 报表业务日期: {report_date}")
    log(f"📤 本次发送: {'群通知' if send_group else ''}"
        f"{' + ' if send_group and send_dm else ''}{'BD 私聊' if send_dm else ''}"
        f"{'（仅多维表格）' if not (send_group or send_dm) else ''}")

    failures: list[str] = []

    # ---- 读 Excel + 分类（失败即中止，后面都没意义）----
    try:
        log("\n📖 读取报表...")
        df_today = original.read_excel_sheet2(args.today)
        df_yesterday = original.read_excel_sheet2(args.yesterday)
        log(f"  今天 {len(df_today)} 条 / 昨天 {len(df_yesterday)} 条")

        log("\n🔄 分析数据...")
        results, notify = original.classify_from_excel(df_today, df_yesterday, report_date)
    except Exception as e:
        log(f"❌ 读取或分析失败：{e}")
        traceback.print_exc()
        return 1

    log("\n📊 分析结果:")
    any_item = False
    for cat in bd.DAILY_STATUS + bd.WEEKLY_STATUS:
        items = notify.get(cat, [])
        if items:
            any_item = True
            log(f"  {cat}: {len(items)} 个")
    if not any_item:
        log("  今日无需关注的商户变更。")

    # ---- 群通知 ----
    if send_group:
        log("\n📨 发送飞书群通知...")
        try:
            original.send_webhook_notification(notify, report_date)
        except Exception as e:
            log(f"  ❌ 群通知失败: {e}")
            failures.append("群通知")

    # ---- token：多维表格和 BD 私聊共用 ----
    token = None
    try:
        token = original.get_tenant_access_token()
        log("\n🔑 Token 获取成功")
    except Exception as e:
        log(f"\n❌ Token 获取失败，多维表格与 BD 私聊都会跳过: {e}")
        failures.append("Token")

    # ---- 多维表格 ----
    if token:
        log("\n🔗 更新飞书多维表格...")
        try:
            original.sync_to_bitable(token, results)
        except Exception as e:
            log(f"  ❌ 多维表格更新失败: {e}")
            traceback.print_exc()
            failures.append("多维表格")

    # ---- BD 私聊 ----
    if send_dm:
        if token:
            try:
                bd.send_bd_messages(token, notify, report_date)
            except Exception as e:
                log(f"  ❌ BD 私聊失败: {e}")
                traceback.print_exc()
                failures.append("BD私聊")
        else:
            failures.append("BD私聊")

    # ---- 本地 txt ----
    # 存到脚本目录，不要存到工作台的临时上传目录（那个目录没人会去看）
    try:
        original.save_local_report(notify, report_date, SCRIPT_DIR)
    except Exception as e:
        log(f"  ⚠️ 本地报告保存失败: {e}")

    if failures:
        log(f"\n❌ 完成，但以下环节失败：{'、'.join(failures)}")
        return 1
    log("\n✅ 全部完成")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"\n❌ 程序出错: {e}")
        traceback.print_exc()
        sys.exit(1)
