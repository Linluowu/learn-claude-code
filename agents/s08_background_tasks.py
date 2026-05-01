#!/usr/bin/env python3
# 指定脚本解释器为 python3，使该文件可直接在 Unix/Linux 系统上运行

# Harness: background execution -- the model thinks while the harness waits.
# Harness：后台执行 —— 当 harness（框架）在等待时，模型仍然在"思考"（后台任务在运行）。
# 本文件引入 BackgroundManager，支持在后台线程中执行命令，
# 主循环不需要阻塞等待，可以并行处理多个耗时任务。

"""
s08_background_tasks.py - Background Tasks

s08_background_tasks.py - 后台任务

Run commands in background threads. A notification queue is drained
before each LLM call to deliver results.

在后台线程中运行命令。每次调用 LLM 前清空通知队列，将结果注入对话。

    Main thread                Background thread
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

    Timeline:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]        (parallel)
                 |              |
                 +-- notification queue --> [results injected]

Key insight: "Fire and forget -- the agent doesn't block while the command runs."

核心洞察："即发即忘 —— 命令运行时 Agent 不会被阻塞。"
"""

import os
# 导入 os 模块，用于操作系统交互，如读取环境变量

import subprocess
# 导入 subprocess 模块，用于启动新进程并执行系统命令

import threading
# 导入 threading 模块，用于创建和管理线程，实现并发执行

import uuid
# 导入 uuid 模块，用于生成通用唯一标识符，为每个后台任务分配唯一ID

from pathlib import Path
# 从 pathlib 模块导入 Path 类，用于面向对象的路径操作

from anthropic import Anthropic
# 从 anthropic 包导入 Anthropic 类，这是与 Claude API 通信的客户端

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv，用于从 .env 文件加载环境变量

load_dotenv(override=True)
# 加载 .env 文件中的环境变量到 os.environ 中
# override=True 表示如果环境变量已存在，则用 .env 中的值覆盖

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查环境变量 ANTHROPIC_BASE_URL 是否已设置
    # 如果设置了自定义 API 基础 URL（例如使用代理），则：
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 从环境变量中移除 ANTHROPIC_AUTH_TOKEN
    # 原因：当使用自定义 base URL 时，认证方式可能不同，不需要此令牌

WORKDIR = Path.cwd()
# 设置工作目录为当前工作目录
# Path.cwd() 返回当前进程的工作目录

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端实例
# base_url 参数允许指定自定义 API 地址，未设置则使用默认值

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取 MODEL_ID，指定要使用的 Claude 模型版本

SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."
# 设置系统提示词（System Prompt），告诉模型它是一个编程助手，
# 并且可以使用 background_run 工具来执行长时间运行的命令


# -- BackgroundManager: threaded execution + notification queue --
# -- BackgroundManager：线程化执行 + 通知队列 --
class BackgroundManager:
    # BackgroundManager 类：管理后台任务的创建、执行、查询和结果通知
    def __init__(self):
        # 构造函数，初始化 BackgroundManager 实例
        self.tasks = {}
        # self.tasks：字典，存储所有后台任务的信息
        # 键：任务ID（字符串），值：包含状态、结果、命令的字典
        self._notification_queue = []
        # _notification_queue：列表，作为通知队列使用
        # 用于存储已完成任务的结果通知，在每次 LLM 调用前被"清空"（drain）
        self._lock = threading.Lock()
        # _lock：线程锁，用于保护对 _notification_queue 的并发访问
        # 因为多个后台线程可能同时向队列添加通知

    def run(self, command: str) -> str:
        # run 方法：启动一个后台任务
        # 参数 command: str —— 要在后台执行的 shell 命令字符串
        # 返回值 -> str —— 返回任务ID和启动信息
        """Start a background thread, return task_id immediately."""
        # 文档字符串：启动后台线程，立即返回任务ID
        task_id = str(uuid.uuid4())[:8]
        # 生成唯一任务ID：使用 uuid4() 生成 UUID，取前8个字符作为短ID
        # 这样既能保证唯一性，又便于在终端显示和使用
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        # 将新任务记录到 tasks 字典中
        # status: "running" 表示任务正在运行
        # result: None 表示尚无结果
        # command: 保存原始命令字符串，用于后续查询和显示
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        # 创建后台线程：
        # - target=self._execute：线程执行的目标函数
        # - args=(task_id, command)：传递给目标函数的参数
        # - daemon=True：设置为守护线程
        #   守护线程在主线程结束时自动终止，不需要等待它完成
        thread.start()
        # 启动后台线程，任务开始在后台执行
        return f"Background task {task_id} started: {command[:80]}"
        # 立即返回任务启动信息，包含任务ID和命令的前80个字符
        # 注意：此时命令可能还在执行中，结果尚未产生

    def _execute(self, task_id: str, command: str):
        # _execute 方法：线程的目标函数，在后台执行命令
        # 参数 task_id: str —— 任务的唯一标识符
        # 参数 command: str —— 要执行的 shell 命令
        """Thread target: run subprocess, capture output, push to queue."""
        # 文档字符串：线程目标函数：运行子进程，捕获输出，推送到队列
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            # 执行 shell 命令：
            # - shell=True：通过 shell 执行命令
            # - cwd=WORKDIR：在工作目录中执行
            # - capture_output=True：捕获标准输出和标准错误
            # - text=True：以文本模式返回输出
            # - timeout=300：5分钟超时（比前台命令的120秒更长）
            output = (r.stdout + r.stderr).strip()[:50000]
            # 合并 stdout 和 stderr，去除首尾空白，限制50000字符
            status = "completed"
            # 设置状态为已完成
        except subprocess.TimeoutExpired:
            # 捕获超时异常
            output = "Error: Timeout (300s)"
            # 超时输出错误信息
            status = "timeout"
            # 设置状态为超时
        except Exception as e:
            # 捕获其他所有异常
            output = f"Error: {e}"
            # 输出异常信息
            status = "error"
            # 设置状态为错误
        self.tasks[task_id]["status"] = status
        # 更新任务状态
        self.tasks[task_id]["result"] = output or "(no output)"
        # 更新任务结果，如果输出为空则使用 "(no output)"
        with self._lock:
            # 获取锁，保护对通知队列的访问
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })
            # 向通知队列添加完成通知
            # 包含任务ID、状态、命令（截断到80字符）和结果（截断到500字符）

    def check(self, task_id: str = None) -> str:
        # check 方法：查询任务状态
        # 参数 task_id: str = None —— 可选的任务ID，如果不提供则列出所有任务
        # 返回值 -> str —— 任务状态信息
        """Check status of one task or list all."""
        if task_id:
            # 如果提供了任务ID
            t = self.tasks.get(task_id)
            # 从 tasks 字典中查找该任务
            if not t:
                # 如果任务不存在
                return f"Error: Unknown task {task_id}"
                # 返回错误信息
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
            # 返回任务状态、命令（截断到60字符）和结果（如果还在运行则显示"(running)"）
        lines = []
        # 如果没有提供任务ID，列出所有任务
        for tid, t in self.tasks.items():
            # 遍历所有任务
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
            # 格式化每个任务的简要信息
        return "\n".join(lines) if lines else "No background tasks."
        # 返回所有任务的列表，如果没有任务则返回提示信息

    def drain_notifications(self) -> list:
        # drain_notifications 方法：清空并返回所有待处理的通知
        # 返回值 -> list —— 通知列表
        """Return and clear all pending completion notifications."""
        # 文档字符串：返回并清除所有待处理的完成通知
        with self._lock:
            # 获取锁
            notifs = list(self._notification_queue)
            # 复制通知队列的内容
            self._notification_queue.clear()
            # 清空通知队列
        return notifs
        # 返回复制的通知列表
        # 这样主循环可以在每次 LLM 调用前获取所有新完成的通知


BG = BackgroundManager()
# 创建全局 BackgroundManager 实例


# -- Tool implementations --
# -- 工具实现 --
def safe_path(p: str) -> Path:
    # 安全路径函数，确保路径不会逃逸出工作目录
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # 前台执行 bash 命令（阻塞式）
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    # 读取文件内容
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    # 写入文件
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    # 工具处理函数字典，增加了后台任务相关工具
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "background_run":   lambda **kw: BG.run(kw["command"]),
    # background_run 工具：在后台运行命令
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    # check_background 工具：查询后台任务状态
}

TOOLS = [
    # 可用工具列表，增加了后台任务工具
    {"name": "bash", "description": "Run a shell command (blocking).",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "background_run", "description": "Run command in background thread. Returns task_id immediately.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # background_run 工具定义：在后台线程中运行命令，立即返回任务ID
    {"name": "check_background", "description": "Check background task status. Omit task_id to list all.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    # check_background 工具定义：查询后台任务状态，省略 task_id 则列出所有任务
]


def agent_loop(messages: list):
    # Agent 核心循环
    while True:
        # Drain background notifications and inject as system message before LLM call
        # 在调用 LLM 前清空后台通知，并将其作为系统消息注入
        notifs = BG.drain_notifications()
        # 获取所有新完成的后台任务通知
        if notifs and messages:
            # 如果有通知且消息历史不为空
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            # 格式化通知文本，每个通知一行
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            # 将通知作为用户消息追加到对话历史中
            # 使用 <background-results> XML 标签包裹，便于模型识别这是后台任务结果
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 调用 Claude API
        messages.append({"role": "assistant", "content": response.content})
        # 追加模型回复
        if response.stop_reason != "tool_use":
            return
        results = []
        for block in response.content:
            if block.type == "tool_use":
                handler = TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
                print(f"> {block.name}:")
                print(str(output)[:200])
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
