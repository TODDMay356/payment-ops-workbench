import os, sys
from playwright.sync_api import sync_playwright
SP=os.environ.get("WB_FIXTURES") or os.path.expanduser("~/wb-fixtures")
BASE="http://127.0.0.1:5050"; fails=[]
def check(n,c,x=""):
    print(("  PASS " if c else "  FAIL ")+n+(("  << "+str(x)[:200]) if not c and x else ""))
    if not c: fails.append(n)

with sync_playwright() as pw:
    b=pw.chromium.launch(executable_path=os.environ.get("WB_CHROME") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",args=["--no-sandbox"])
    p=b.new_page(viewport={"width":1180,"height":1000}); errs=[]
    p.on("pageerror",lambda e:errs.append(str(e)))

    print("\n[1] 待跑状态")
    p.goto(BASE,wait_until="networkidle"); p.wait_for_timeout(1800)
    note=p.inner_text("#omInboxNote")
    print("   ",note.replace("\n"," "))
    check("说明文件已备好","已备好" in note and "2026-08-02" in note,note)
    check("有开始运行按钮",p.locator("#omInboxNote button:has-text('开始运行')").count()==1)
    check("提示会发群通知","会发群通知" in note,note)
    check("没写『自动跑完』","自动跑完" not in note and "自动运行" not in note,note)
    p.screenshot(path=f"{SP}/om-todo.png",full_page=True)

    print("\n[2] 点按钮跑")
    p.click("#omInboxNote button")
    for _ in range(40):
        if "退出码" in p.inner_text("#omState"): break
        p.wait_for_timeout(500)
    st=p.inner_text("#omState"); out=p.inner_text("#omOut")
    print("    state:",st)
    for l in out.split("\n")[:5]: print("     |",l)
    check("跑成功","退出码 0" in st,st)
    check("用的是存档文件而非临时上传","data/inbox/order_monitor/2026-08-02" in out,out[:250])
    check("日志说明了用的哪两天","2026-08-02 / 2026-08-01" in out,out[:250])

    print("\n[3] 跑完后卡片翻成已完成")
    p.wait_for_timeout(2500)
    note2=p.inner_text("#omInboxNote")
    print("   ",note2.replace("\n"," "))
    check("显示已跑完","已跑完" in note2,note2)
    check("按钮变成再跑一次",p.locator("#omInboxNote button:has-text('再跑一次')").count()==1,note2)

    print("\n[4] 没配密码时弹框问一次")
    p2=b.new_page(); e2=[]
    p2.on("pageerror",lambda e:e2.append(str(e)))
    asked=[]
    p2.on("dialog", lambda d:(asked.append(d.message), d.accept("typed-pw")))
    p2.goto(BASE,wait_until="networkidle"); p2.wait_for_timeout(1500)
    # 让服务端这一次没有密码可用
    p2.evaluate("""() => {
      const orig = window.fetch;
      window.fetch = (u, o) => {
        if (typeof u === 'string' && u.includes('/api/inbox/run-order-monitor')) {
          const body = JSON.parse((o && o.body) || '{}');
          if (!body.password) return Promise.resolve(new Response(
            JSON.stringify({ok:false, msg:'需要脚本密码：在 mailbox.json 的 order_monitor.password 里填上'}),
            {status:400, headers:{'Content-Type':'application/json'}}));
        }
        return orig(u, o);
      };
    }""")
    p2.click("#omInboxNote button")
    for _ in range(40):
        if "退出码" in p2.inner_text("#omState"): break
        p2.wait_for_timeout(500)
    print("    弹框:",asked)
    print("    state:",p2.inner_text("#omState"))
    check("弹框问了密码",len(asked)==1 and "密码" in asked[0],asked)
    check("输了密码就跑成功","退出码 0" in p2.inner_text("#omState"),p2.inner_text("#omState"))
    check("无 JS 报错",not e2 and not errs,e2+errs)
    b.close()
print("\n"+("全部通过" if not fails else f"失败 {len(fails)}: {fails}")); sys.exit(1 if fails else 0)
