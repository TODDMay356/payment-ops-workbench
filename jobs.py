"""
jobs.py - 后台任务运行器

出单监控原来用 subprocess.Popen(creationflags=CREATE_NEW_CONSOLE) 弹一个独立 CMD 窗口，
起完就立刻 return {"ok": True} —— 工作台其实并不知道脚本成没成功，弹不弹窗只是表象。
这里换成：进程在后台跑，stdout 逐行读进内存，网页通过 SSE 实时看到日志和退出码。

用法：
    job_id = start_job("order-monitor", [sys.executable, "x.py", "--mode", "both"], cwd=".")
    snapshot(job_id)                      # 取当前全量日志 + 状态
    for chunk in stream(job_id): ...      # 阻塞式逐行产出，进程退出后自然结束
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict

# 内存里最多保留多少个历史任务（超出淘汰最旧的已结束任务）
MAX_JOBS = 20
# 单个任务最多保留多少行日志，防止跑飞的脚本吃光内存
MAX_LINES = 5000

_jobs: "OrderedDict[str, Job]" = OrderedDict()
_jobs_lock = threading.Lock()


class Job:
    def __init__(self, job_id: str, name: str, argv: list[str]):
        self.id = job_id
        self.name = name
        self.argv = argv
        self.lines: list[str] = []
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.returncode: int | None = None
        self.error: str | None = None
        self.proc: subprocess.Popen | None = None
        # 有新行或状态变化时 set，等待者被唤醒后自己 clear
        self.tick = threading.Event()
        self.lock = threading.Lock()

    # ---------- 内部：写入 ----------

    def _append(self, line: str):
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > MAX_LINES:
                drop = len(self.lines) - MAX_LINES
                del self.lines[:drop]
        self.tick.set()

    def _finish(self, returncode: int | None, error: str | None = None):
        with self.lock:
            self.returncode = returncode
            self.error = error
            self.finished_at = time.time()
        self.tick.set()

    # ---------- 外部：读取 ----------

    @property
    def running(self) -> bool:
        return self.finished_at is None

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "running": self.finished_at is None,
                "returncode": self.returncode,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "next_index": len(self.lines),
                "lines": self.lines[since:],
            }


def _reader(job: Job):
    """把子进程 stdout 逐行搬进 job.lines。stderr 已经并进 stdout。"""
    assert job.proc is not None and job.proc.stdout is not None
    try:
        for raw in job.proc.stdout:
            job._append(raw.rstrip("\r\n"))
    except Exception as e:  # 管道意外断开也要让等待者收到结束信号
        job._append(f"[工作台] 读取脚本输出失败：{e}")
    finally:
        try:
            job.proc.stdout.close()
        except Exception:
            pass
        rc = job.proc.wait()
        job._finish(rc)


def _evict_locked():
    """只淘汰已经结束的任务，正在跑的永远保留。"""
    while len(_jobs) > MAX_JOBS:
        for jid, j in list(_jobs.items()):
            if not j.running:
                del _jobs[jid]
                break
        else:
            return  # 全在跑，不动


def start_job(name: str, argv: list[str], cwd: str | None = None,
              env_extra: dict[str, str] | None = None) -> str:
    """起一个后台进程，返回 job_id。不会弹任何窗口。"""
    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id, name, argv)

    env = os.environ.copy()
    # 关键：Windows 上 Python 对管道 stdout 是块缓冲的，不设这个，
    # 「实时日志」会退化成脚本跑完才一次性吐出来。
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)

    kwargs: dict = {}
    if sys.platform == "win32":
        # 明确不要新控制台窗口 —— 这正是原来 CREATE_NEW_CONSOLE 不稳定的地方
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    with _jobs_lock:
        _jobs[job_id] = job
        _evict_locked()

    try:
        job.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,   # 脚本里若残留 input() 会立刻 EOF，而不是永远挂住
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
    except Exception as e:
        job._append(f"[工作台] 启动失败：{e}")
        job._finish(None, error=str(e))
        return job_id

    threading.Thread(target=_reader, args=(job,), name=f"job-{job_id}", daemon=True).start()
    return job_id


def get(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def snapshot(job_id: str, since: int = 0) -> dict | None:
    job = get(job_id)
    return job.snapshot(since) if job else None


def stream(job_id: str, since: int = 0, heartbeat: float = 15.0):
    """
    阻塞式生成器，逐行产出 (index, line)；进程结束后产出 ("done", snapshot) 然后返回。
    heartbeat 秒内没有新行就产出一次 ("ping", None)，避免代理/浏览器掐掉空闲连接。
    """
    job = get(job_id)
    if job is None:
        return
    idx = since
    while True:
        # 先 clear 再读快照：否则「读完快照」到「开始等待」之间新来的行会把 tick 置位，
        # 而我们随后又把它清掉，等待者就丢掉了这次唤醒，日志会卡住直到心跳超时。
        job.tick.clear()
        with job.lock:
            total = len(job.lines)
            pending = job.lines[idx:total]
            done = job.finished_at is not None
        for line in pending:
            yield ("line", line)
        idx = total

        if done:
            # 进程已结束且缓冲区已排空
            yield ("done", job.snapshot(since=idx))
            return

        if not job.tick.wait(heartbeat):
            yield ("ping", None)
