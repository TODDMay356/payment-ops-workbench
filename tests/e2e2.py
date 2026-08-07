import os
import sys
from playwright.sync_api import sync_playwright
SP=os.environ.get("WB_FIXTURES") or os.path.expanduser("~/wb-fixtures")
BASE="http://127.0.0.1:5050"; fails=[]
def check(n,c,x=""):
    print(("  PASS " if c else "  FAIL ")+n+(("  << "+str(x)[:200]) if not c and x else ""))
    if not c: fails.append(n)

with sync_playwright() as pw:
    b=pw.chromium.launch(executable_path=os.environ.get("WB_CHROME") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",args=["--no-sandbox"])

    print("\n[A] 手动选文件仍然能解析（不回归）")
    p=b.new_page(); errs=[]; p.on("pageerror",lambda e:errs.append(str(e)))
    p.goto(BASE+"/conversion",wait_until="networkidle"); p.wait_for_timeout(1500)
    p.set_input_files("#file", f"{SP}/real.xlsx")
    for _ in range(60):
        if p.locator("#loadMsg .banner.ok").count()>0: break
        p.wait_for_timeout(1000)
    msg=p.inner_text("#loadMsg")
    print("    ",msg.replace("\n"," ")[:110])
    check("手动上传解析成功","已解析" in msg and "real.xlsx" in msg,msg)
    check("手动路径无 JS 报错",not errs,errs)

    print("\n[B] 出单监控手动表单仍然能跑（不回归）")
    p2=b.new_page(); e2=[]; p2.on("pageerror",lambda e:e2.append(str(e)))
    # 自己把 D-1 挪走，别依赖上一个用例留下的状态
    import os, shutil
    ARCH=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"data/inbox/order_monitor/2026-08-01.xlsx")
    moved = os.path.exists(ARCH)
    if moved: shutil.move(ARCH, ARCH+".bak")
    p2.goto(BASE,wait_until="networkidle"); p2.wait_for_timeout(1500)
    om=p2.inner_text("#omInboxNote")
    print("    omNote:",om.replace("\n"," "))
    check("缺 D-1 时出黄条说明","缺 2026-08-01" in om,om)
    p2.set_input_files('input[name="today"]',  f"{SP}/maildrop/新商户审核耗时报表2026-08-02.xlsx")
    p2.set_input_files('input[name="yesterday"]',f"{SP}/maildrop/新商户审核耗时报表2026-08-01.xlsx")
    p2.fill("#omDate","2026-08-02")
    p2.fill('input[name="password"]',"testpw")
    p2.evaluate("document.querySelector('#omForm').requestSubmit()")
    for _ in range(40):
        if "退出码" in p2.inner_text("#omState"): break
        p2.wait_for_timeout(500)
    st=p2.inner_text("#omState"); out=p2.inner_text("#omOut")
    print("    state:",st)
    for l in out.split("\n")[:6]: print("     |",l)
    check("手动跑成功", "退出码 0" in st, st)
    check("用的是上传的临时文件而非存档", "wb_order_" in out, out[:200])
    check("首页无 JS 报错",not e2,e2)
    if moved: shutil.move(ARCH+".bak", ARCH)
    b.close()
print("\n"+("全部通过" if not fails else f"失败 {len(fails)}: {fails}")); sys.exit(1 if fails else 0)
