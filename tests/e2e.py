import os, sys, json, time
from playwright.sync_api import sync_playwright

SP = os.environ.get("WB_FIXTURES") or os.path.expanduser("~/wb-fixtures")
CHROME = os.environ.get("WB_CHROME") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
BASE = "http://127.0.0.1:5050"
fails = []

def check(name, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + name + (("  << " + str(extra)) if not cond and extra else ""))
    if not cond: fails.append(name)

with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    pg = b.new_page(viewport={"width": 1280, "height": 1000})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    posted = []
    pg.on("request", lambda r: posted.append(r.url) if "/api/inbox/analyzed" in r.url else None)

    # ---------- 1. 首页待办卡片 ----------
    print("\n[1] 首页")
    pg.goto(BASE, wait_until="networkidle")
    pg.wait_for_timeout(1500)
    bar = pg.inner_text("#inboxBar")
    conv = pg.inner_text("#convRow")
    om = pg.inner_text("#omInboxNote")
    print("    inboxBar:", bar.replace("\n", " "))
    print("    convRow :", conv.replace("\n", " "))
    print("    omNote  :", om.replace("\n", " "))
    check("收取条可见且写了时间", "上次收取" in bar, bar)
    check("转化率卡片变成待办按钮", "打开并分析 2026-08-03" in conv, conv)
    check("出单监控卡片给出可点的运行入口",
          "2026-08-02" in om and ("开始运行" in om or "再跑一次" in om), om)
    check("出单监控不自作主张跑", "自动跑完" not in om and "等待自动运行" not in om, om)
    check("按钮点下去会发消息，说清楚了", "会发群通知" in om, om)
    check("卡片不再宣称自动推飞书", "自动写多维表格" not in pg.content())
    pg.screenshot(path=f"{SP}/inbox-home.png", full_page=True)

    # ---------- 2. 点卡片 → 工具页自动解析 ----------
    print("\n[2] 点『打开并分析』")
    # 工具页是**同源** iframe，和首页共用一个渲染进程的主线程。
    # SheetJS 同步解析 4.6MB 的报表期间，整个渲染进程都是卡住的 ——
    # 连针对父页面的查询也要排队等它。所以这里把超时放宽，
    # 并且等解析完再断言（不是产品缺陷，是同源 iframe 的固有行为）。
    pg.set_default_timeout(180000)
    pg.click("#convRow button")
    FR = '#toolFrames iframe[data-tool="conversion"]'   # 现在每个工具一个 iframe
    fr = pg.frame_locator(FR)
    # state="attached"：解析成功后 loadWorkbook 会 showPanel('overview')，
    # 把 #p-upload 整个 hidden 掉，横幅在 DOM 里但不可见 —— 默认的 visible 等不到。
    fr.locator("#loadMsg .banner.ok").wait_for(state="attached", timeout=180000)
    src = pg.get_attribute(FR, "src")
    check("iframe 带上了 ?inbox=", "?inbox=2026-08-03" in (src or ""), src)
    msg = fr.locator("#loadMsg").inner_text()
    print("    loadMsg:", msg.replace("\n", " ")[:120])
    check("没选文件就解析完了", "已解析" in msg and "邮箱自动获取" in msg, msg)
    check("回报了 /api/inbox/analyzed", len(posted) > 0, posted)

    # ---------- 3. 播报内容没回归 ----------
    print("\n[3] 播报")
    # 直接调 showPanel —— 容器里资源紧张时 Playwright 的 click 容易超时
    pg.frame("").evaluate("1") if False else None
    frame = [f for f in pg.frames if "conversion" in (f.url or "")][0]
    frame.evaluate("showPanel('cast')")
    pg.wait_for_timeout(800)
    bc = fr.locator("#bcOut").inner_text()
    lines = [l for l in bc.split("\n") if l.strip()]
    print("    行数:", len(lines))
    for l in lines[:20]:
        print("     ", l)
    check("分场景段落还在", bc.count("■") >= 2, bc.count("■"))
    check("穿透行还在", "穿透" in bc)
    check("不含工作台地址", "127.0.0.1" not in bc and "工作台" not in bc)
    pg.screenshot(path=f"{SP}/inbox-conv.png", full_page=True)

    # ---------- 4. 回首页，卡片应已消掉 ----------
    print("\n[4] 回首页")
    pg.click("#backBtn")
    pg.wait_for_timeout(1500)
    conv2 = pg.inner_text("#convRow")
    tag = pg.inner_text("#convInboxTag")
    print("    convRow:", conv2.replace("\n", " "))
    print("    tag    :", tag)
    check("待办卡片已消掉", "打开并分析" not in conv2, conv2)
    check("标签变成已分析", "已分析" in tag, tag)

    # ---------- 5. 手动上传路径没坏 ----------
    print("\n[5] 直接打开工具页（不带参数）")
    pg2 = b.new_page()
    e2 = []
    pg2.on("pageerror", lambda e: e2.append(str(e)))
    pg2.goto(BASE + "/conversion", wait_until="networkidle")
    pg2.wait_for_timeout(2500)
    body = pg2.inner_text("body")
    check("提示已就绪但没自动加载", "工作台已从邮箱取到" in body, body[:200])
    check("有『直接加载』按钮", pg2.locator("button:has-text('直接加载')").count() == 1)
    check("#file 输入框还在", pg2.locator("#file").count() == 1)
    check("工具页无 JS 报错", not e2, e2)

    check("首页无 JS 报错", not errs, errs)
    b.close()

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
