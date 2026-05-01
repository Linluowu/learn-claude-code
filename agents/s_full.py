#!/usr/bin/env python3
# 指定脚本解释器为 python3

# Harness: all mechanisms combined -- the complete cockpit for the model.
# Harness：所有机制组合 —— 模型的完整驾驶舱。
# 本文件是 capstone（巅峰之作）实现，结合了 s01-s11 的所有机制。
# 注意：s12（任务感知的 worktree 隔离）单独教学，未包含在此文件中。

"""
s_full.py - Full Reference Agent

s_full.py - 完整参考智能体

Capstone implementation combining every mechanism from s01-s11.
Session s12 (task-aware worktree isolation) is taught separately.
NOT a teaching session -- this is the "put it all together" reference.

巅峰实现，结合了 s01-s11 的所有机制。
会话 s12（任务感知的 worktree 隔离）单独教学。
不是教学会话 —— 这是"把所有东西放在一起"的参考实现。

    +------------------------------------------------------------------+
    |                        FULL AGENT                                 |
    |                                                                   |
    |  System prompt (s05 skills, task-first + optional todo nag)      |
    |                                                                   |
    |  Before each LLM call:                                            |
    |  +--------------------+  +------------------+  +--------------+  |
    |  | Microcompact (s06) |  | Drain bg (s08)   |  | Check inbox  |  |
    |  | Auto-compact (s06) |  | notifications    |  | (s09)        |  |
    |  +--------------------+  +------------------+  +--------------+  |
    |                                                                   |
    |  Tool dispatch (s02 pattern):                                     |
    |  +--------+----------+----------+---------+-----------+          |
    |  | bash   | read     | write    | edit    | TodoWrite |          |
    |  | task   | load_sk  | compress | bg_run  | bg_check  |          |
    |  | t_crt  | t_get    | t_upd    | t_list  | spawn_tm  |          |
    |  | list_tm| send_msg | rd_inbox | bcast   | shutdown  |          |
    |  | plan   | idle     | claim    |         |           |          |
    |  +--------+----------+----------+---------+-----------+          |
    |                                                                   |
    |  Subagent (s04):  spawn -> work -> return summary                 |
    |  Teammate (s09):  spawn -> work -> idle -> auto-claim (s11)      |
    |  Shutdown (s10):  request_id handshake                            |
    |  Plan gate (s10): submit -> approve/reject                        |
    +------------------------------------------------------------------+

    REPL commands: /compact /tasks /team /inbox
"""

import json
# 导入 json 模块：用于序列化和反序列化 JSON 数据

import os
# 导入 os 模块：用于操作系统交互

import re
# 导入 re 模块：用于正则表达式，解析 SKILL.md 的 frontmatter

import subprocess
# 导入 subprocess 模块：用于执行外部命令

import threading
# 导入 threading 模块：用于多线程，后台任务和队友线程

import time
# 导入 time 模块：用于时间戳和轮询等待

import uuid
# 导入 uuid 模块：用于生成唯一标识符

from pathlib import Path
# 从 pathlib 导入 Path：用于现代路径操作

from queue import Queue
# 从 queue 导入 Queue：用于后台任务通知队列（线程安全）

from anthropic import Anthropic
# 从 anthropic 导入 Anthropic：Anthropic SDK，与 Claude API 通信

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv：加载 .env 文件中的环境变量

load_dotenv(override=True)
# 加载环境变量，override=True 覆盖已存在的变量

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查是否设置了自定义 API 基础 URL
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 如果设置了，移除认证令牌

WORKDIR = Path.cwd()
# 设置工作目录为当前目录

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取模型 ID

# 配置各种目录路径
TEAM_DIR = WORKDIR / ".team"
# 团队配置目录：.team/

INBOX_DIR = TEAM_DIR / "inbox"
# 收件箱目录：.team/inbox/

TASKS_DIR = WORKDIR / ".tasks"
# 任务目录：.tasks/

SKILLS_DIR = WORKDIR / "skills"
# 技能目录：skills/

TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# 对话记录目录：.transcripts/

TOKEN_THRESHOLD = 100000
# TOKEN_THRESHOLD：自动压缩的 token 阈值（比 s06 的 50000 更高）

POLL_INTERVAL = 5
# POLL_INTERVAL：空闲轮询间隔（秒）

IDLE_TIMEOUT = 60
# IDLE_TIMEOUT：空闲超时（秒）

VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}
# 有效的消息类型集合


# === SECTION: base_tools ===
# === 部分：基础工具 ===
def safe_path(p: str) -> Path:
    # safe_path 函数：安全检查文件路径，防止路径遍历攻击
    # 参数 p: str —— 用户提供的相对路径字符串
    # 返回值 -> Path —— 经过安全检查的绝对路径
    path = (WORKDIR / p).resolve()
    # 将相对路径转换为绝对路径：
    # - WORKDIR / p：使用 Path 的 / 运算符拼接工作目录和用户路径
    # - .resolve()：解析为绝对路径，消除 .. 和 . 等符号
    if not path.is_relative_to(WORKDIR):
        # 检查解析后的路径是否仍相对于 WORKDIR
        raise ValueError(f"Path escapes workspace: {p}")
        # 路径逃逸则抛出 ValueError
    return path
    # 返回安全的绝对路径

def run_bash(command: str) -> str:
    # run_bash 函数：执行 bash 命令
    # 参数 command: str —— 要执行的 shell 命令字符串
    # 返回值 -> str —— 命令执行的输出结果
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 危险命令黑名单
    if any(d in command for d in dangerous):
        # 检查命令中是否包含任何危险关键词
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # 执行命令：
        # - shell=True：通过 shell 执行
        # - cwd=WORKDIR：在工作目录中执行
        # - capture_output=True：捕获输出
        # - text=True：以文本模式返回
        # - timeout=120：120 秒超时
        out = (r.stdout + r.stderr).strip()
        # 合并 stdout 和 stderr，去除首尾空白
        return out[:50000] if out else "(no output)"
        # 返回输出（限制 50000 字符）
    except subprocess.TimeoutExpired:
        # 超时异常
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int = None) -> str:
    # run_read 函数：读取文件内容
    # 参数 path: str —— 文件路径
    # 参数 limit: int = None —— 可选行数限制
    # 返回值 -> str —— 文件内容
    try:
        lines = safe_path(path).read_text().splitlines()
        # 安全读取文件并按行分割
        if limit and limit < len(lines):
            # 如果超出限制
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
            # 截取并添加省略提示
        return "\n".join(lines)[:50000]
        # 返回内容
    except Exception as e:
        # 异常
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    # run_write 函数：写入文件
    # 参数 path: str —— 文件路径
    # 参数 content: str —— 文件内容
    # 返回值 -> str —— 操作结果
    try:
        fp = safe_path(path)
        # 获取安全路径
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 创建父目录
        fp.write_text(content)
        # 写入内容
        return f"Wrote {len(content)} bytes to {path}"
        # 返回成功信息
    except Exception as e:
        # 异常
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # run_edit 函数：编辑文件
    # 参数 path: str —— 文件路径
    # 参数 old_text: str —— 要替换的文本
    # 参数 new_text: str —— 新文本
    # 返回值 -> str —— 操作结果
    try:
        fp = safe_path(path)
        # 获取安全路径
        c = fp.read_text()
        # 读取内容
        if old_text not in c:
            # 如果原始文本不存在
            return f"Error: Text not found in {path}"
            # 返回错误
        fp.write_text(c.replace(old_text, new_text, 1))
        # 替换文本（只替换第一次出现）
        return f"Edited {path}"
        # 返回成功信息
    except Exception as e:
        # 异常
        return f"Error: {e}"


# === SECTION: todos (s03) ===
# === 部分：待办事项 (s03) ===
class TodoManager:
    # TodoManager 类：管理待办事项列表
    def __init__(self):
        # 构造函数
        self.items = []
        # 待办事项列表

    def update(self, items: list) -> str:
        # update 方法：更新待办事项列表
        # 参数 items: list —— 模型传入的新待办事项列表
        # 返回值 -> str —— 渲染后的待办事项列表
        validated, ip = [], 0
        # validated：验证通过的列表，ip：进行中计数
        for i, item in enumerate(items):
            # 遍历每个待办事项
            content = str(item.get("content", "")).strip()
            # 提取内容
            status = str(item.get("status", "pending")).lower()
            # 提取状态
            af = str(item.get("activeForm", "")).strip()
            # 提取 activeForm（进行中的描述）
            if not content: raise ValueError(f"Item {i}: content required")
            # 内容不能为空
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status '{status}'")
            # 状态必须有效
            if not af: raise ValueError(f"Item {i}: activeForm required")
            # activeForm 不能为空
            if status == "in_progress": ip += 1
            # 如果在进行中，计数
            validated.append({"content": content, "status": status, "activeForm": af})
            # 添加到验证列表
        if len(validated) > 20: raise ValueError("Max 20 todos")
        # 最多 20 个待办事项
        if ip > 1: raise ValueError("Only one in_progress allowed")
        # 只能有一个进行中的任务
        self.items = validated
        # 保存验证后的列表
        return self.render()
        # 返回渲染结果

    def render(self) -> str:
        # render 方法：渲染待办事项列表
        # 返回值 -> str —— 格式化字符串
        if not self.items: return "No todos."
        # 空列表
        lines = []
        # 输出行列表
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            # 根据状态选择标记
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            # 如果在进行中，显示 activeForm
            lines.append(f"{m} {item['content']}{suffix}")
            # 格式化行
        done = sum(1 for t in self.items if t["status"] == "completed")
        # 统计已完成数量
        lines.append(f"\n({done}/{len(self.items)} completed)")
        # 添加进度统计
        return "\n".join(lines)
        # 返回格式化字符串

    def has_open_items(self) -> bool:
        # has_open_items 方法：检查是否有未完成的待办事项
        # 返回值 -> bool —— 是否有未完成的项
        return any(item.get("status") != "completed" for item in self.items)
        # 如果有任何项的状态不是 completed，返回 True


# === SECTION: subagent (s04) ===
# === 部分：子智能体 (s04) ===
def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    # run_subagent 函数：运行子智能体
    # 参数 prompt: str —— 子智能体的任务提示词
    # 参数 agent_type: str = "Explore" —— 子智能体类型，默认 Explore
    # 返回值 -> str —— 子智能体的摘要结果
    sub_tools = [
        {"name": "bash", "description": "Run command.",
         "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
        {"name": "read_file", "description": "Read file.",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    ]
    # 子智能体的基础工具：bash 和 read_file
    if agent_type != "Explore":
        # 如果不是 Explore 类型，添加 write_file 和 edit_file 工具
        sub_tools += [
            {"name": "write_file", "description": "Write file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
        ]
    sub_handlers = {
        "bash": lambda **kw: run_bash(kw["command"]),
        "read_file": lambda **kw: run_read(kw["path"]),
        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    }
    # 子智能体的工具处理函数
    sub_msgs = [{"role": "user", "content": prompt}]
    # 子智能体的消息历史，从空开始（上下文隔离）
    resp = None
    # 最后一个响应
    for _ in range(30):
        # 最多 30 轮
        resp = client.messages.create(model=MODEL, messages=sub_msgs, tools=sub_tools, max_tokens=8000)
        # 调用 Claude API
        sub_msgs.append({"role": "assistant", "content": resp.content})
        # 追加模型回复
        if resp.stop_reason != "tool_use":
            # 如果没有调用工具
            break
            # 跳出循环
        results = []
        # 结果列表
        for b in resp.content:
            # 遍历内容块
            if b.type == "tool_use":
                # 如果是工具调用
                h = sub_handlers.get(b.name, lambda **kw: "Unknown tool")
                # 查找处理函数
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(h(**b.input))[:50000]})
                # 执行工具并构建结果
        sub_msgs.append({"role": "user", "content": results})
        # 追加结果
    if resp:
        # 如果有响应
        return "".join(b.text for b in resp.content if hasattr(b, "text")) or "(no summary)"
        # 提取所有文本并返回
    return "(subagent failed)"
    # 失败则返回错误信息


# === SECTION: skills (s05) ===
# === 部分：技能 (s05) ===
class SkillLoader:
    # SkillLoader 类：扫描并加载技能
    def __init__(self, skills_dir: Path):
        # 构造函数
        # 参数 skills_dir: Path —— 技能目录
        self.skills = {}
        # 技能字典
        if skills_dir.exists():
            # 如果目录存在
            for f in sorted(skills_dir.rglob("SKILL.md")):
                # 遍历所有 SKILL.md 文件
                text = f.read_text()
                # 读取内容
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                # 解析 frontmatter
                meta, body = {}, text
                # 初始化元数据和正文
                if match:
                    # 如果匹配到 frontmatter
                    for line in match.group(1).strip().splitlines():
                        # 遍历 frontmatter 的每一行
                        if ":" in line:
                            # 如果包含冒号
                            k, v = line.split(":", 1)
                            # 分割键值对
                            meta[k.strip()] = v.strip()
                            # 保存到元数据字典
                    body = match.group(2).strip()
                    # 提取正文
                name = meta.get("name", f.parent.name)
                # 获取技能名称
                self.skills[name] = {"meta": meta, "body": body}
                # 保存技能

    def descriptions(self) -> str:
        # descriptions 方法：生成技能描述列表
        # 返回值 -> str —— 格式化的描述字符串
        if not self.skills: return "(no skills)"
        # 空列表
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())
        # 返回格式化的描述

    def load(self, name: str) -> str:
        # load 方法：加载指定技能的完整内容
        # 参数 name: str —— 技能名称
        # 返回值 -> str —— 技能的完整内容
        s = self.skills.get(name)
        # 查找技能
        if not s: return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        # 未找到则返回错误
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"
        # 返回包装在 XML 标签中的技能内容


# === SECTION: compression (s06) ===
# === 部分：压缩 (s06) ===
def estimate_tokens(messages: list) -> int:
    # estimate_tokens 函数：粗略估计消息列表的 token 数量
    # 参数 messages: list —— 消息历史列表
    # 返回值 -> int —— 估计的 token 数
    return len(json.dumps(messages, default=str)) // 4
    # 假设平均每个 token 约 4 个字符

def microcompact(messages: list):
    # microcompact 函数：微压缩，替换旧的工具结果
    # 参数 messages: list —— 消息历史列表（原地修改）
    indices = []
    # 工具结果的索引列表
    for i, msg in enumerate(messages):
        # 遍历所有消息
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            # 如果是用户消息且内容是列表
            for part in msg["content"]:
                # 遍历每个部分
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    # 如果是工具结果
                    indices.append(part)
                    # 添加到列表
    if len(indices) <= 3:
        # 如果不超过 3 个
        return
        # 不需要压缩
    for part in indices[:-3]:
        # 遍历除最后 3 个之外的所有工具结果
        if isinstance(part.get("content"), str) and len(part["content"]) > 100:
            # 如果内容是字符串且长度超过 100
            part["content"] = "[cleared]"
            # 替换为占位符

def auto_compact(messages: list) -> list:
    # auto_compact 函数：自动压缩对话
    # 参数 messages: list —— 消息历史列表
    # 返回值 -> list —— 压缩后的消息列表
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    # 创建对话记录目录
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    # 生成记录文件路径
    with open(path, "w") as f:
        # 打开文件
        for msg in messages:
            # 遍历每条消息
            f.write(json.dumps(msg, default=str) + "\n")
            # 写入 JSONL 格式
    conv_text = json.dumps(messages, default=str)[-80000:]
    # 取最后 80000 字符
    resp = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        max_tokens=2000,
    )
    # 调用 Claude API 生成摘要
    summary = resp.content[0].text
    # 提取摘要文本
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
    ]
    # 返回只包含摘要的单条消息列表


# === SECTION: file_tasks (s07) ===
# === 部分：文件任务 (s07) ===
class TaskManager:
    # TaskManager 类：管理持久化任务
    def __init__(self):
        # 构造函数
        TASKS_DIR.mkdir(exist_ok=True)
        # 创建任务目录

    def _next_id(self) -> int:
        # _next_id 方法：获取下一个任务 ID
        # 返回值 -> int —— 下一个 ID
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        # 提取所有任务 ID
        return max(ids, default=0) + 1
        # 返回最大 ID + 1

    def _load(self, tid: int) -> dict:
        # _load 方法：加载指定任务
        # 参数 tid: int —— 任务 ID
        # 返回值 -> dict —— 任务字典
        p = TASKS_DIR / f"task_{tid}.json"
        # 构建文件路径
        if not p.exists(): raise ValueError(f"Task {tid} not found")
        # 不存在则抛出错误
        return json.loads(p.read_text())
        # 读取并解析 JSON

    def _save(self, task: dict):
        # _save 方法：保存任务到文件
        # 参数 task: dict —— 任务字典
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2))
        # 序列化为 JSON 并写入

    def create(self, subject: str, description: str = "") -> str:
        # create 方法：创建新任务
        # 参数 subject: str —— 任务主题
        # 参数 description: str = "" —— 任务描述
        # 返回值 -> str —— 新任务的 JSON 字符串
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": []}
        # 构建任务字典
        self._save(task)
        # 保存
        return json.dumps(task, indent=2)
        # 返回 JSON

    def get(self, tid: int) -> str:
        # get 方法：获取任务详情
        # 参数 tid: int —— 任务 ID
        # 返回值 -> str —— 任务 JSON 字符串
        return json.dumps(self._load(tid), indent=2)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        # update 方法：更新任务
        # 参数 tid: int —— 任务 ID
        # 参数 status: str = None —— 新状态
        # 参数 add_blocked_by: list = None —— 要添加的阻塞任务
        # 参数 remove_blocked_by: list = None —— 要移除的阻塞任务
        # 返回值 -> str —— 更新后的 JSON
        task = self._load(tid)
        # 加载任务
        if status:
            # 如果提供了新状态
            task["status"] = status
            # 更新状态
            if status == "completed":
                # 如果完成
                for f in TASKS_DIR.glob("task_*.json"):
                    # 遍历所有任务
                    t = json.loads(f.read_text())
                    # 读取任务
                    if tid in t.get("blockedBy", []):
                        # 如果当前任务阻塞了其他任务
                        t["blockedBy"].remove(tid)
                        # 移除阻塞关系
                        self._save(t)
                        # 保存更新
            if status == "deleted":
                # 如果删除
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                # 删除文件
                return f"Task {tid} deleted"
                # 返回结果
        if add_blocked_by:
            # 如果添加阻塞
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
            # 合并并去重
        if remove_blocked_by:
            # 如果移除阻塞
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
            # 过滤
        self._save(task)
        # 保存
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        # list_all 方法：列出所有任务
        # 返回值 -> str —— 格式化的任务列表
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        # 读取所有任务
        if not tasks: return "No tasks."
        # 空列表
        lines = []
        # 输出行
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            # 状态标记
            owner = f" @{t['owner']}" if t.get("owner") else ""
            # 负责人
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            # 阻塞信息
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
            # 格式化行
        return "\n".join(lines)
        # 返回格式化列表

    def claim(self, tid: int, owner: str) -> str:
        # claim 方法：认领任务
        # 参数 tid: int —— 任务 ID
        # 参数 owner: str —— 认领者
        # 返回值 -> str —— 认领结果
        task = self._load(tid)
        # 加载任务
        task["owner"] = owner
        # 设置负责人
        task["status"] = "in_progress"
        # 更新状态为进行中
        self._save(task)
        # 保存
        return f"Claimed task #{tid} for {owner}"
        # 返回结果


# === SECTION: background (s08) ===
# === 部分：后台任务 (s08) ===
class BackgroundManager:
    # BackgroundManager 类：管理后台任务的执行
    def __init__(self):
        # 构造函数
        self.tasks = {}
        # 任务字典，存储所有后台任务
        self.notifications = Queue()
        # 通知队列，使用线程安全的 Queue

    def run(self, command: str, timeout: int = 120) -> str:
        # run 方法：启动后台任务
        # 参数 command: str —— 要执行的命令
        # 参数 timeout: int = 120 —— 超时时间
        # 返回值 -> str —— 任务启动信息
        tid = str(uuid.uuid4())[:8]
        # 生成任务 ID
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        # 记录任务
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        # 启动后台线程
        return f"Background task {tid} started: {command[:80]}"
        # 返回启动信息

    def _exec(self, tid: str, command: str, timeout: int):
        # _exec 方法：线程目标函数，执行命令
        # 参数 tid: str —— 任务 ID
        # 参数 command: str —— 命令
        # 参数 timeout: int —— 超时时间
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            # 执行命令
            output = (r.stdout + r.stderr).strip()[:50000]
            # 捕获输出
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
            # 更新状态为已完成
        except Exception as e:
            # 异常
            self.tasks[tid].update({"status": "error", "result": str(e)})
            # 更新状态为错误
        self.notifications.put({"task_id": tid, "status": self.tasks[tid]["status"],
                                "result": self.tasks[tid]["result"][:500]})
        # 向通知队列添加通知

    def check(self, tid: str = None) -> str:
        # check 方法：查询任务状态
        # 参数 tid: str = None —— 任务 ID
        # 返回值 -> str —— 状态信息
        if tid:
            # 如果指定了任务 ID
            t = self.tasks.get(tid)
            # 查找任务
            return f"[{t['status']}] {t.get('result') or '(running)'}" if t else f"Unknown: {tid}"
            # 返回状态
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "No bg tasks."
        # 返回所有任务

    def drain(self) -> list:
        # drain 方法：清空并返回所有通知
        # 返回值 -> list —— 通知列表
        notifs = []
        # 通知列表
        while not self.notifications.empty():
            # 当队列不为空时
            notifs.append(self.notifications.get_nowait())
            # 获取通知
        return notifs
        # 返回列表


# === SECTION: messaging (s09) ===
# === 部分：消息系统 (s09) ===
class MessageBus:
    # MessageBus 类：管理队友间的消息发送和接收
    def __init__(self):
        # 构造函数
        INBOX_DIR.mkdir(parents=True, exist_ok=True)
        # 创建收件箱目录

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # send 方法：发送消息
        # 参数 sender: str —— 发送者
        # 参数 to: str —— 接收者
        # 参数 content: str —— 内容
        # 参数 msg_type: str = "message" —— 消息类型
        # 参数 extra: dict = None —— 额外字段
        # 返回值 -> str —— 发送结果
        msg = {"type": msg_type, "from": sender, "content": content,
               "timestamp": time.time()}
        # 构建消息字典
        if extra: msg.update(extra)
        # 合并额外字段
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            # 打开收件箱文件（追加模式）
            f.write(json.dumps(msg) + "\n")
            # 写入 JSONL
        return f"Sent {msg_type} to {to}"
        # 返回结果

    def read_inbox(self, name: str) -> list:
        # read_inbox 方法：读取并清空收件箱
        # 参数 name: str —— 队友名称
        # 返回值 -> list —— 消息列表
        path = INBOX_DIR / f"{name}.jsonl"
        # 构建文件路径
        if not path.exists(): return []
        # 不存在则返回空列表
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        # 读取并解析所有消息
        path.write_text("")
        # 清空文件
        return msgs
        # 返回消息列表

    def broadcast(self, sender: str, content: str, names: list) -> str:
        # broadcast 方法：广播消息
        # 参数 sender: str —— 发送者
        # 参数 content: str —— 内容
        # 参数 names: list —— 队友名称列表
        # 返回值 -> str —— 广播结果
        count = 0
        # 计数器
        for n in names:
            # 遍历所有队友
            if n != sender:
                # 不发送给自己
                self.send(sender, n, content, "broadcast")
                # 发送广播
                count += 1
                # 计数
        return f"Broadcast to {count} teammates"
        # 返回结果


# === SECTION: shutdown + plan tracking (s10) ===
# === 部分：关闭和计划追踪 (s10) ===
shutdown_requests = {}
# 关闭请求追踪器
plan_requests = {}
# 计划请求追踪器


# === SECTION: team (s09/s11) ===
# === 部分：团队 (s09/s11) ===
class TeammateManager:
    # TeammateManager 类：管理队友的创建和生命周期
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        # 构造函数
        # 参数 bus: MessageBus —— 消息总线
        # 参数 task_mgr: TaskManager —— 任务管理器
        TEAM_DIR.mkdir(exist_ok=True)
        # 创建团队目录
        self.bus = bus
        # 保存消息总线
        self.task_mgr = task_mgr
        # 保存任务管理器
        self.config_path = TEAM_DIR / "config.json"
        # 配置文件路径
        self.config = self._load()
        # 加载配置
        self.threads = {}
        # 线程字典

    def _load(self) -> dict:
        # _load 方法：加载配置
        # 返回值 -> dict —— 配置字典
        if self.config_path.exists():
            # 如果文件存在
            return json.loads(self.config_path.read_text())
            # 读取并解析
        return {"team_name": "default", "members": []}
        # 返回默认配置

    def _save(self):
        # _save 方法：保存配置
        self.config_path.write_text(json.dumps(self.config, indent=2))
        # 写入文件

    def _find(self, name: str) -> dict:
        # _find 方法：查找队友
        # 参数 name: str —— 队友名称
        # 返回值 -> dict —— 队友字典，不存在返回 None
        for m in self.config["members"]:
            # 遍历所有成员
            if m["name"] == name: return m
            # 找到则返回
        return None
        # 未找到

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # spawn 方法：创建队友
        # 参数 name: str —— 队友名称
        # 参数 role: str —— 角色
        # 参数 prompt: str —— 初始提示词
        # 返回值 -> str —— 创建结果
        member = self._find(name)
        # 查找队友
        if member:
            # 如果存在
            if member["status"] not in ("idle", "shutdown"):
                # 如果正在工作中
                return f"Error: '{name}' is currently {member['status']}"
                # 返回错误
            member["status"] = "working"
            # 更新状态
            member["role"] = role
            # 更新角色
        else:
            # 如果不存在
            member = {"name": name, "role": role, "status": "working"}
            # 创建新队友
            self.config["members"].append(member)
            # 添加到列表
        self._save()
        # 保存配置
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        # 启动线程
        return f"Spawned '{name}' (role: {role})"
        # 返回结果

    def _set_status(self, name: str, status: str):
        # _set_status 方法：设置队友状态
        # 参数 name: str —— 队友名称
        # 参数 status: str —— 新状态
        member = self._find(name)
        # 查找队友
        if member:
            # 如果存在
            member["status"] = status
            # 更新状态
            self._save()
            # 保存

    def _loop(self, name: str, role: str, prompt: str):
        # _loop 方法：队友主循环
        # 参数 name: str —— 队友名称
        # 参数 role: str —— 角色
        # 参数 prompt: str —— 初始提示词
        team_name = self.config["team_name"]
        # 获取团队名称
        sys_prompt = (f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
                      f"Use idle when done with current work. You may auto-claim tasks.")
        # 系统提示词
        messages = [{"role": "user", "content": prompt}]
        # 初始化消息历史
        tools = [
            {"name": "bash", "description": "Run command.", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Edit file.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message.", "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}}, "required": ["to", "content"]}},
            {"name": "idle", "description": "Signal no more work.", "input_schema": {"type": "object", "properties": {}}},
            # idle 工具
            {"name": "claim_task", "description": "Claim task by ID.", "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
            # claim_task 工具
        ]
        while True:
            # 无限循环
            # -- WORK PHASE --
            # -- 工作阶段 --
            for _ in range(50):
                # 最多 50 轮
                inbox = self.bus.read_inbox(name)
                # 读取收件箱
                for msg in inbox:
                    # 处理消息
                    if msg.get("type") == "shutdown_request":
                        # 如果是关闭请求
                        self._set_status(name, "shutdown")
                        # 更新状态
                        return
                        # 结束线程
                    messages.append({"role": "user", "content": json.dumps(msg)})
                    # 追加消息
                try:
                    response = client.messages.create(
                        model=MODEL, system=sys_prompt, messages=messages,
                        tools=tools, max_tokens=8000)
                    # 调用 Claude API
                except Exception:
                    self._set_status(name, "shutdown")
                    # 异常则关闭
                    return
                messages.append({"role": "assistant", "content": response.content})
                # 追加回复
                if response.stop_reason != "tool_use":
                    # 如果没有调用工具
                    break
                    # 跳出工作阶段
                results = []
                # 结果列表
                idle_requested = False
                # idle 请求标记
                for block in response.content:
                    # 遍历内容块
                    if block.type == "tool_use":
                        # 如果是工具调用
                        if block.name == "idle":
                            # 如果是 idle 工具
                            idle_requested = True
                            # 标记
                            output = "Entering idle phase."
                            # 设置输出
                        elif block.name == "claim_task":
                            # 如果是 claim_task 工具
                            output = self.task_mgr.claim(block.input["task_id"], name)
                            # 认领任务
                        elif block.name == "send_message":
                            # 如果是 send_message 工具
                            output = self.bus.send(name, block.input["to"], block.input["content"])
                            # 发送消息
                        else:
                            # 其他工具
                            dispatch = {"bash": lambda **kw: run_bash(kw["command"]),
                                        "read_file": lambda **kw: run_read(kw["path"]),
                                        "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
                                        "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"])}
                            # 工具调度字典
                            output = dispatch.get(block.name, lambda **kw: "Unknown")(**block.input)
                            # 执行工具
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        # 打印结果
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                        # 构建结果
                messages.append({"role": "user", "content": results})
                # 追加结果
                if idle_requested:
                    # 如果请求了 idle
                    break
                    # 跳出工作阶段
            # -- IDLE PHASE: poll for messages and unclaimed tasks --
            # -- 空闲阶段：轮询消息和未认领任务 --
            self._set_status(name, "idle")
            # 更新状态为 idle
            resume = False
            # 恢复标记
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                # 轮询多次
                time.sleep(POLL_INTERVAL)
                # 等待
                inbox = self.bus.read_inbox(name)
                # 读取收件箱
                if inbox:
                    # 如果有消息
                    for msg in inbox:
                        # 处理
                        if msg.get("type") == "shutdown_request":
                            # 如果是关闭请求
                            self._set_status(name, "shutdown")
                            # 更新状态
                            return
                            # 结束线程
                        messages.append({"role": "user", "content": json.dumps(msg)})
                        # 追加消息
                    resume = True
                    # 标记恢复
                    break
                    # 跳出轮询
                unclaimed = []
                # 未认领任务列表
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    # 遍历所有任务
                    t = json.loads(f.read_text())
                    # 读取任务
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        # 如果是未认领的 pending 任务
                        unclaimed.append(t)
                        # 添加到列表
                if unclaimed:
                    # 如果有未认领任务
                    task = unclaimed[0]
                    # 取第一个
                    self.task_mgr.claim(task["id"], name)
                    # 认领任务
                    # Identity re-injection for compressed contexts
                    # 上下文压缩后的身份重新注入
                    if len(messages) <= 3:
                        # 如果消息历史很短（可能是压缩后）
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        # 在开头插入身份块
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                        # 插入确认消息
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    # 追加任务提示词
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    # 追加确认消息
                    resume = True
                    # 标记恢复
                    break
                    # 跳出轮询
            if not resume:
                # 如果没有恢复（超时）
                self._set_status(name, "shutdown")
                # 更新状态为 shutdown
                return
                # 结束线程
            self._set_status(name, "working")
            # 更新状态为 working，继续工作阶段

    def list_all(self) -> str:
        # list_all 方法：列出所有队友
        # 返回值 -> str —— 格式化的队友列表
        if not self.config["members"]: return "No teammates."
        # 空列表
        lines = [f"Team: {self.config['team_name']}"]
        # 第一行显示团队名
        for m in self.config["members"]:
            # 遍历成员
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
            # 格式化
        return "\n".join(lines)
        # 返回

    def member_names(self) -> list:
        # member_names 方法：返回所有队友名称
        # 返回值 -> list —— 名称列表
        return [m["name"] for m in self.config["members"]]
        # 列表推导式


# === SECTION: global_instances ===
# === 部分：全局实例 ===
TODO = TodoManager()
# 全局 TodoManager 实例
SKILLS = SkillLoader(SKILLS_DIR)
# 全局 SkillLoader 实例
TASK_MGR = TaskManager()
# 全局 TaskManager 实例
BG = BackgroundManager()
# 全局 BackgroundManager 实例
BUS = MessageBus()
# 全局 MessageBus 实例
TEAM = TeammateManager(BUS, TASK_MGR)
# 全局 TeammateManager 实例

# === SECTION: system_prompt ===
# === 部分：系统提示词 ===
SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Prefer task_create/task_update/task_list for multi-step work. Use TodoWrite for short checklists.
Use task for subagent delegation. Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""
# 系统提示词：综合了所有功能


# === SECTION: shutdown_protocol (s10) ===
# === 部分：关闭协议 (s10) ===
def handle_shutdown_request(teammate: str) -> str:
    # handle_shutdown_request 函数：处理关闭请求
    # 参数 teammate: str —— 目标队友
    # 返回值 -> str —— 请求结果
    req_id = str(uuid.uuid4())[:8]
    # 生成请求 ID
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    # 记录请求
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    # 发送关闭请求
    return f"Shutdown request {req_id} sent to '{teammate}'"
    # 返回结果

# === SECTION: plan_approval (s10) ===
# === 部分：计划审批 (s10) ===
def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    # handle_plan_review 函数：处理计划审批
    # 参数 request_id: str —— 请求 ID
    # 参数 approve: bool —— 是否批准
    # 参数 feedback: str = "" —— 反馈
    # 返回值 -> str —— 审批结果
    req = plan_requests.get(request_id)
    # 查找请求
    if not req: return f"Error: Unknown plan request_id '{request_id}'"
    # 不存在则返回错误
    req["status"] = "approved" if approve else "rejected"
    # 更新状态
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    # 发送响应
    return f"Plan {req['status']} for '{req['from']}'"
    # 返回结果


# === SECTION: tool_dispatch (s02) ===
# === 部分：工具调度 (s02) ===
TOOL_HANDLERS = {
    # 工具处理函数字典，包含所有工具
    "bash":             lambda **kw: run_bash(kw["command"]),
    "read_file":        lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":       lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":        lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    # TodoWrite 工具
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    # task 工具：创建子智能体
    "load_skill":       lambda **kw: SKILLS.load(kw["name"]),
    # load_skill 工具
    "compress":         lambda **kw: "Compressing...",
    # compress 工具
    "background_run":   lambda **kw: BG.run(kw["command"], kw.get("timeout", 120)),
    # background_run 工具
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
    # check_background 工具
    "task_create":      lambda **kw: TASK_MGR.create(kw["subject"], kw.get("description", "")),
    # task_create 工具
    "task_get":         lambda **kw: TASK_MGR.get(kw["task_id"]),
    # task_get 工具
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("remove_blocked_by")),
    # task_update 工具
    "task_list":        lambda **kw: TASK_MGR.list_all(),
    # task_list 工具
    "spawn_teammate":   lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    # spawn_teammate 工具
    "list_teammates":   lambda **kw: TEAM.list_all(),
    # list_teammates 工具
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    # send_message 工具
    "read_inbox":       lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    # read_inbox 工具
    "broadcast":        lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    # broadcast 工具
    "shutdown_request": lambda **kw: handle_shutdown_request(kw["teammate"]),
    # shutdown_request 工具
    "plan_approval":    lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    # plan_approval 工具
    "idle":             lambda **kw: "Lead does not idle.",
    # idle 工具（队长不需要 idle）
    "claim_task":       lambda **kw: TASK_MGR.claim(kw["task_id"], "lead"),
    # claim_task 工具
}

TOOLS = [
    # 可用工具列表（25 个工具）
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "TodoWrite", "description": "Update task tracking list.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "activeForm": {"type": "string"}}, "required": ["content", "status", "activeForm"]}}, "required": ["items"]}},
    # TodoWrite 工具定义
    {"name": "task", "description": "Spawn a subagent for isolated exploration or work.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]}}, "required": ["prompt"]}},
    # task 工具定义
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "compress", "description": "Manually compress conversation context.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "background_run", "description": "Run command in background thread.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer"}}, "required": ["command"]}},
    {"name": "check_background", "description": "Check background task status.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "string"}}}},
    {"name": "task_create", "description": "Create a persistent file task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    {"name": "task_get", "description": "Get task details by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
    {"name": "task_update", "description": "Update task status or dependencies.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]}, "add_blocked_by": {"type": "array", "items": {"type": "integer"}}, "remove_blocked_by": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    {"name": "task_list", "description": "List all tasks.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "spawn_teammate", "description": "Spawn a persistent autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check shutdown request status.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "Enter idle state.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


# === SECTION: agent_loop ===
# === 部分：Agent 循环 ===
def agent_loop(messages: list):
    # agent_loop 函数：完整的 Agent 核心循环
    # 参数 messages: list —— 消息历史列表
    rounds_without_todo = 0
    # rounds_without_todo：自上次使用 TodoWrite 以来的轮数
    while True:
        # 无限循环
        # s06: compression pipeline
        # s06：压缩管道
        microcompact(messages)
        # 执行微压缩
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            # 如果 token 超过阈值
            print("[auto-compact triggered]")
            # 打印触发信息
            messages[:] = auto_compact(messages)
            # 执行自动压缩
        # s08: drain background notifications
        # s08：清空后台通知
        notifs = BG.drain()
        # 获取所有后台通知
        if notifs:
            # 如果有通知
            txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            # 格式化通知
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
            # 追加到消息历史
        # s10: check lead inbox
        # s10：检查队长收件箱
        inbox = BUS.read_inbox("lead")
        # 读取收件箱
        if inbox:
            # 如果有消息
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>"})
            # 追加到消息历史
        # LLM call
        # LLM 调用
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 调用 Claude API
        messages.append({"role": "assistant", "content": response.content})
        # 追加模型回复
        if response.stop_reason != "tool_use":
            # 如果没有调用工具
            return
            # 结束循环
        # Tool execution
        # 工具执行
        results = []
        # 结果列表
        used_todo = False
        # TodoWrite 使用标记
        manual_compress = False
        # 手动压缩标记
        for block in response.content:
            # 遍历内容块
            if block.type == "tool_use":
                # 如果是工具调用
                if block.name == "compress":
                    # 如果是 compress 工具
                    manual_compress = True
                    # 标记手动压缩
                handler = TOOL_HANDLERS.get(block.name)
                # 查找处理函数
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    # 执行工具
                except Exception as e:
                    # 异常
                    output = f"Error: {e}"
                print(f"> {block.name}:")
                # 打印工具名
                print(str(output)[:200])
                # 打印输出
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                # 构建结果
                if block.name == "TodoWrite":
                    # 如果使用了 TodoWrite
                    used_todo = True
                    # 标记
        # s03: nag reminder (only when todo workflow is active)
        # s03：唠叨提醒（仅当待办工作流激活时）
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        # 更新计数器
        if TODO.has_open_items() and rounds_without_todo >= 3:
            # 如果有未完成的待办事项且超过 3 轮未更新
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
            # 注入提醒
        messages.append({"role": "user", "content": results})
        # 追加结果到消息历史
        # s06: manual compress
        # s06：手动压缩
        if manual_compress:
            # 如果触发了手动压缩
            print("[manual compact]")
            # 打印信息
            messages[:] = auto_compact(messages)
            # 执行压缩
            return
            # 结束循环


# === SECTION: repl ===
# === 部分：REPL ===
if __name__ == "__main__":
    # 当脚本直接运行时执行
    history = []
    # 初始化空的消息历史
    while True:
        # 无限循环
        try:
            query = input("\033[36ms_full >> \033[0m")
            # 显示青色提示符 "s_full >> "，接收用户输入
        except (EOFError, KeyboardInterrupt):
            # 捕获 EOF 或中断
            break
            # 退出循环
        if query.strip().lower() in ("q", "exit", ""):
            # 检查退出命令
            break
            # 退出循环
        if query.strip() == "/compact":
            # 处理 /compact 命令：手动压缩
            if history:
                # 如果有历史记录
                print("[manual compact via /compact]")
                # 打印信息
                history[:] = auto_compact(history)
                # 执行压缩
            continue
            # 跳过本次循环
        if query.strip() == "/tasks":
            # 处理 /tasks 命令：显示所有任务
            print(TASK_MGR.list_all())
            # 打印任务列表
            continue
            # 跳过
        if query.strip() == "/team":
            # 处理 /team 命令：显示所有队友
            print(TEAM.list_all())
            # 打印队友列表
            continue
            # 跳过
        if query.strip() == "/inbox":
            # 处理 /inbox 命令：显示队长收件箱
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            # 打印收件箱
            continue
            # 跳过
        history.append({"role": "user", "content": query})
        # 追加用户输入到历史
        agent_loop(history)
        # 启动 Agent 循环
        response_content = history[-1]["content"]
        # 获取最后一条消息内容
        if isinstance(response_content, list):
            # 如果是列表
            for block in response_content:
                # 遍历每个块
                if hasattr(block, "text"):
                    # 如果有 text 属性
                    print(block.text)
                    # 打印文本
        print()
        # 打印空行
