#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 总结（本地 / 服务器端运行，无浏览器 CORS 问题）
====================================================
配合网页「复制 AI 提示词」按钮使用，分工：网页做分析，Python 做 AI 总结。

用法（三选一）：
  1) 网页点「复制 AI 提示词」→ 粘到一个文件 prompt.txt → 运行：
        python ai_summary.py prompt.txt
  2) 直接吃剪贴板（macOS 举例）：
        pbpaste | python ai_summary.py
        # Windows PowerShell:  Get-Clipboard | python ai_summary.py
        # Linux:               xclip -o -sel clip | python ai_summary.py
  3) 直接运行后手动粘贴，粘完按 Ctrl-D（Windows 为 Ctrl-Z 回车）结束：
        python ai_summary.py

结果会打印到屏幕，并写入 ai_summary.txt（再贴回网页或直接用都行）。

配置（都可用环境变量覆盖，无需改代码）：
  DEEPSEEK_API_KEY   API Key（不填则运行时询问，输入不回显）
  AI_BASE_URL        接口地址，默认 https://api.deepseek.com（OpenAI 兼容）
  AI_MODEL           模型名，默认 deepseek-chat（换成你账号支持的即可）
换成通义 / Moonshot / 火山方舟等 OpenAI 兼容服务：改 AI_BASE_URL / AI_MODEL 即可，逻辑不用动。
"""
import os
import sys
import json
import getpass

try:
    import requests
except ImportError:
    sys.exit("缺少依赖，请先安装：pip install requests")

BASE_URL = os.environ.get("AI_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = os.environ.get("AI_MODEL", "deepseek-chat")
API_KEY_ENV = "DEEPSEEK_API_KEY"
TIMEOUT_SECONDS = 120
MAX_TOKENS = 2000
SYSTEM_PROMPT = "你是一名专业、谨慎、只依据给定数据说话的支付数据分析师。"
OUTPUT_FILE = "ai_summary.txt"


def read_prompt() -> str:
    """从 命令行文件参数 / 管道 / 手动粘贴 三种方式读取提示词。"""
    if len(sys.argv) > 1:
        path = sys.argv[1]
        if not os.path.exists(path):
            sys.exit(f"找不到文件：{path}")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    if not sys.stdin.isatty():          # 有管道输入
        return sys.stdin.read()
    print("请粘贴网页复制的 AI 提示词，粘完后按 Ctrl-D（Windows 为 Ctrl-Z 回车）结束：")
    return sys.stdin.read()


def get_api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        print(f"已从环境变量 {API_KEY_ENV} 读取 API Key", file=sys.stderr)
        return key
    return getpass.getpass(
        f"输入 API Key（未检测到环境变量 {API_KEY_ENV}，输入时不显示）："
    ).strip()


def call_ai(prompt: str, api_key: str) -> str:
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    print(f"正在调用 {MODEL} @ {BASE_URL} …", file=sys.stderr)
    try:
        resp = requests.post(
            BASE_URL + "/chat/completions",
            headers=headers,
            data=json.dumps(payload),
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        sys.exit(f"网络错误：{e}")
    if resp.status_code != 200:
        sys.exit(f"接口返回 {resp.status_code}：{resp.text[:500]}")
    try:
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, ValueError):
        sys.exit(f"返回结果格式异常：{resp.text[:500]}")


def main():
    prompt = read_prompt().strip()
    if not prompt:
        sys.exit("提示词为空，已退出。")
    api_key = get_api_key()
    if not api_key:
        sys.exit("未提供 API Key，已退出。")

    content = call_ai(prompt, api_key)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    print("\n" + "=" * 60)
    print(content)
    print("=" * 60)
    print(f"\n已保存到 {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
