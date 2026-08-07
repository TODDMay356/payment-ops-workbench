import os
"""交互改动的核心场景：切走再回来，数据还在。"""
import os, sys
from playwright.sync_api import sync_playwright
SP=os.environ.get("WB_FIXTURES") or os.path.expanduser("~/wb-fixtures")
BASE="http://127.0.0.1:5050"; fails=[]
CONV='#toolFrames iframe[data-tool="conversion"]'
MERCH='#toolFrames iframe[data-tool="merchant"]'
def check(n,c,x=""):
    print(("  PASS " if c else "  FAIL ")+n+(("  << "+str(x)[:180]) if not c and x else ""))
    if not c: fails.append(n)

with sync_playwright() as pw:
    b=pw.chromium.launch(executable_path=os.environ.get("WB_CHROME") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",args=["--no-sandbox"])
    p=b.new_page(viewport={"width":1280,"height":950}); errs=[]
    p.on("pageerror",lambda e:errs.append(str(e)))
    p.set_default_timeout(180000)
    p.goto(BASE,wait_until="networkidle"); p.wait_for_timeout(2000)

    print("\n[1] 商户密码在首页")
    check("首页有密码输入", p.locator("#maPw").is_visible())
    check("标签写着需要密码", "需要密码" in p.inner_text("#maTag"))
    check("页脚不再有商户入口", "商户成功率做深挖" not in p.inner_text(".foot"))
    p.fill("#maPw","wrong"); p.click("#maGo"); p.wait_for_timeout(600)
    check("输错有提示", "密码不对" in p.inner_text("#maErr"), p.inner_text("#maErr"))

    print("\n[2] 解锁后直接进，没有锁屏")
    p.fill("#maPw","pay2026"); p.click("#maGo"); p.wait_for_timeout(2500)
    check("进了商户工具", p.locator(MERCH).count()==1)
    mf=p.frame_locator(MERCH)
    check("没有锁屏", mf.locator("#lock").count()==0)

    print("\n[3] 转化率：从首页卡片进去解析")
    p.click("#backBtn"); p.wait_for_timeout(1200)
    check("解锁后卡片变成打开按钮", "打开商户成功率分析" in p.inner_text("#maForm"))
    p.click("#convRow button")
    cf=p.frame_locator(CONV)
    cf.locator("#loadMsg .banner.ok").wait_for(state="attached", timeout=180000)
    tdate = cf.locator("#loadMsg").inner_text()
    print("    ", tdate.replace("\n"," ")[:100])
    check("解析成功", "已解析" in tdate)
    bc_before = cf.locator("#bcOut").inner_text()

    print("\n[4] ★ 切到商户再切回来，数据还在")
    p.click('.rail button[data-nav="merchant"]'); p.wait_for_timeout(1500)
    check("商户可见", p.locator(MERCH).is_visible())
    check("转化率被藏起来但还在", p.locator(CONV).count()==1 and not p.locator(CONV).is_visible())
    p.click('.rail button[data-nav="conversion"]'); p.wait_for_timeout(1500)
    msg2 = cf.locator("#loadMsg").inner_text()
    bc_after = cf.locator("#bcOut").inner_text()
    check("转化率数据还在（没退回上传态）", "已解析" in msg2, msg2[:120])
    check("播报内容一字未变", bc_after == bc_before and len(bc_after)>100)

    print("\n[5] ★ 回首页再进来，数据还在")
    p.click("#backBtn"); p.wait_for_timeout(1200)
    p.click('.rail button[data-nav="conversion"]'); p.wait_for_timeout(1200)
    check("回首页往返后仍在", "已解析" in cf.locator("#loadMsg").inner_text())
    check("播报仍一致", cf.locator("#bcOut").inner_text() == bc_before)

    print("\n[6] 轨道状态点")
    p.click("#backBtn"); p.wait_for_timeout(1000)
    check("转化率有状态点", p.locator('.rail button[data-nav="conversion"] .dot').count()==1)
    check("商户有状态点", p.locator('.rail button[data-nav="merchant"] .dot').count()==1)
    check("首页没有状态点", p.locator('.rail button[data-nav="home"] .dot').count()==0)

    print("\n[7] 同一天不重复加载")
    src_before = p.get_attribute(CONV,"src")
    p.click("#convRow button") if p.locator("#convRow button").count() else None
    p.wait_for_timeout(1500)
    check("src 没变（没重新加载）", p.get_attribute(CONV,"src")==src_before, src_before)

    check("无 JS 报错", not errs, errs)
    p.click("#backBtn"); p.wait_for_timeout(800)
    p.screenshot(path=f"{SP}/interact-home.png", full_page=True)
    b.close()
print("\n"+("全部通过" if not fails else f"失败 {len(fails)}: {fails}")); sys.exit(1 if fails else 0)
