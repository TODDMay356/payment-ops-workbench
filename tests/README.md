# 验证脚本

四套 Playwright 用例。改完 `templates/index.html` / `static/*.html` 一定要跑一遍 ——
这几个页面里没有单元测试，逻辑和 UI 都靠这些用例兜。

| 脚本 | 兜什么 | 大约耗时 |
|---|---|---|
| `e2e.py` | 首页待办卡片 → 点进转化率 → 用邮箱存档自动解析 → 播报内容 → 卡片消掉 | 3–5 分钟 |
| `e2e2.py` | **不回归**：手动选文件仍能解析；出单监控手动表单仍能跑；缺 D-1 时的黄条 | 2–3 分钟 |
| `e2e3.py` | 出单监控「用 X 的报表开始运行」按钮、跑完翻成「再跑一次」、没配密码时弹框 | 1–2 分钟 |
| `e2e4.py` | **切走不丢数据**：转化率 → 商户 → 转回来，播报逐字节一致；轨道状态点；商户密码 | 2–3 分钟 |

慢的是 `e2e.py`：它用真实的 4.6MB 报表在无头浏览器里完整解析一遍，SheetJS 那段是同步的，
几十秒起步。而且工具页是**同源 iframe**，解析时整个渲染进程卡住，
Playwright 针对父页面的查询也得排队等它 —— 所以用例里超时放宽到了 180 秒，那不是卡死。

---

## 跑之前要准备的

测试数据**没进仓库**（里面是真实商户数据），得自己放：

```
$WB_FIXTURES/                       # 默认 ~/wb-fixtures，可用环境变量覆盖
├── real.xlsx                       # 一份真实的转化率报表
├── maildrop/                       # 假装是邮箱，provider=folder 扫这里
│   ├── FlyPay支付转化率统计表20260803.xlsx     # 就是 real.xlsx 的副本
│   ├── 新商户审核耗时报表2026-08-01.xlsx        # 占位文件即可，只验编排
│   └── 新商户审核耗时报表2026-08-02.xlsx
└── omfake/
    └── 出单监控_workbench.py        # 替身脚本，回显参数后 exit 0
```

`omfake/出单监控_workbench.py` 就是个替身，用来确认工作台把**正确的路径、业务日期、
mode、AppSecret** 传了下去，不真的跑分析：

```python
import argparse, os, sys
p = argparse.ArgumentParser()
for a in ("--today","--yesterday","--password","--date","--mode","--app-secret"):
    p.add_argument(a)
a = p.parse_args()
print("today=%s" % a.today, flush=True)
print("yesterday=%s" % a.yesterday, flush=True)
print("date=%s mode=%s password=%s" % (a.date, a.mode, a.password), flush=True)
print("WB_FEISHU_APP_SECRET=%s" % os.environ.get("WB_FEISHU_APP_SECRET",""), flush=True)
print("OK", flush=True); sys.exit(0)
```

出单监控那两个 xlsx 用任意合法 zip 占位就行（`zipfile` 写一个空条目）。

---

## 跑

```bash
cd workbench
pip install playwright                      # 只装 python 包，别跑 playwright install
export WB_FIXTURES=~/wb-fixtures
export WB_CHROME=/opt/pw-browsers/chromium-1194/chrome-linux/chrome   # 按实际改

# 造 config.json / mailbox.json（指向 fixtures），起服务
python app.py &
curl -X POST http://127.0.0.1:5050/api/inbox/fetch

python tests/e2e4.py && python tests/e2e.py && python tests/e2e3.py && python tests/e2e2.py
```

`mailbox.json` 用 `provider: "folder"`、`folder.dir` 指向 `maildrop`；
`config.json` 的 `order_monitor.dir` 指向 `omfake`。

**每套之间要清 `data/inbox/state.json` 里的 `runs` / `analyzed`**，
否则上一套留下的「已分析」「已跑过」状态会让下一套走到别的分支上（踩过这个坑：
第二次跑 `e2e.py` 六项全挂，查半天发现是第一次跑完把 08-03 标成已分析了）。

```bash
python - <<'PY'
import json, pathlib
p = pathlib.Path('data/inbox/state.json'); d = json.loads(p.read_text())
d.pop('runs', None); d.pop('analyzed', None)
p.write_text(json.dumps(d, ensure_ascii=False, indent=2))
PY
```

---

## 写用例时踩过的坑

- **`wait_for()` 默认等「可见」**。解析成功后 `loadWorkbook()` 会 `showPanel('overview')`，
  把 `#p-upload` 整个 `hidden` 掉 —— 成功横幅在 DOM 里但不可见，
  默认的 visible 永远等不到。要用 `state="attached"`
- **别用 `pkill -f "app.py"`**：那个模式会匹配到跑测试的 shell 自己，把自己杀掉。
  用 `ps -eo pid,args | awk '$2=="python3" && $3~/app\.py$/'` 挑 PID
- **iframe 选择器**：现在每个工具一个 iframe，选择器是
  `#toolFrames iframe[data-tool="conversion"]`，不再是 `#toolFrame`
