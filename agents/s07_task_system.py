#!/usr/bin/env python3
# 指定解释器为 python3。

# Harness: persistent tasks -- goals that outlive any single conversation.
# Harness：持久化任务 —— 超越单次对话的生命周期的目标。
# 本文件引入 TaskManager，将任务持久化为 JSON 文件存储在 .tasks/ 目录中，
# 使任务状态能够承受上下文压缩，在对话压缩后仍然存在。

"""
s07_task_system.py - Tasks

s07_task_system.py - 任务系统

Tasks persist as JSON files in .tasks/ so they survive context compression.
Each task has a dependency graph (blockedBy).

任务以 JSON 文件形式持久化存储在 .tasks/ 目录中，因此能够承受上下文压缩。
每个任务都有一个依赖图（blockedBy）。

    .tasks/
      task_1.json  {"id":1, "subject":"...", "status":"completed", ...}
      task_2.json  {"id":2, "blockedBy":[1], "status":"pending", ...}
      task_3.json  {"id":3, "blockedBy":[2], ...}

    Dependency resolution:
    +----------+     +----------+     +----------+
    | task 1   | --> | task 2   | --> | task 3   |
    | complete |     | blocked  |     | blocked  |
    +----------+     +----------+     +----------+
         |                ^
         +--- completing task 1 removes it from task 2's blockedBy

Key insight: "State that survives compression -- because it's outside the conversation."

核心洞察："能够承受压缩的状态 —— 因为它存在于对话之外。"
"""

import json
# 导入 json 模块。

import os
# 导入 os 模块。

import subprocess
# 导入 subprocess 模块。

from pathlib import Path
# 从 pathlib 导入 Path。

from anthropic import Anthropic
# 从 anthropic 导入 Anthropic。

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv。

load_dotenv(override=True)
# 加载环境变量。

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查自定义 base URL。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 移除认证令牌。

WORKDIR = Path.cwd()
# 设置工作目录。

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic 客户端。

MODEL = os.environ["MODEL_ID"]
# 读取模型 ID。

TASKS_DIR = WORKDIR / ".tasks"
# 任务存储目录：工作目录下的 .tasks 隐藏文件夹。
# 每个任务是一个 JSON 文件，以 task_<id>.json 命名。

SYSTEM = f"You are a coding agent at {WORKDIR}. Use task tools to plan and track work."
# 系统提示词：指示使用任务工具规划和跟踪工作。


# -- TaskManager: CRUD with dependency graph, persisted as JSON files --
# -- TaskManager：支持依赖图的 CRUD 操作，以 JSON 文件持久化 --
class TaskManager:
    # TaskManager 类：管理持久化任务的增删改查和依赖关系。
    def __init__(self, tasks_dir: Path):
        # 构造函数。
        # 参数 tasks_dir: Path —— 任务存储目录的 Path 对象。
        self.dir = tasks_dir
        # 保存任务目录。
        self.dir.mkdir(exist_ok=True)
        # 创建任务目录（如果不存在）。
        self._next_id = self._max_id() + 1
        # 初始化下一个任务 ID：当前最大 ID + 1。
        # 这样新创建的任务会自动递增 ID，避免冲突。

    def _max_id(self) -> int:
        # _max_id 方法：查找当前最大任务 ID。
        # 返回值 -> int —— 最大任务 ID，如果没有任务则返回 0。
        ids = [int(f.stem.split("_")[1]) for f in self.dir.glob("task_*.json")]
        # 遍历所有 task_*.json 文件，提取 ID：
        # - self.dir.glob("task_*.json")：查找所有任务文件。
        # - f.stem：文件名（不含扩展名），如 "task_5"。
        # - .split("_")[1]：按下划线分割，取第二部分（ID 数字）。
        # - int(...)：将字符串转为整数。
        return max(ids) if ids else 0
        # 如果有任务文件，返回最大 ID；否则返回 0。

    def _load(self, task_id: int) -> dict:
        # _load 方法：加载指定 ID 的任务。
        # 参数 task_id: int —— 任务 ID。
        # 返回值 -> dict —— 任务字典。
        path = self.dir / f"task_{task_id}.json"
        # 构建任务文件路径。
        if not path.exists():
            # 检查文件是否存在。
            raise ValueError(f"Task {task_id} not found")
            # 不存在则抛出错误。
        return json.loads(path.read_text())
        # 读取文件并解析 JSON，返回任务字典。

    def _save(self, task: dict):
        # _save 方法：保存任务到文件。
        # 参数 task: dict —— 任务字典。
        path = self.dir / f"task_{task['id']}.json"
        # 构建文件路径。
        path.write_text(json.dumps(task, indent=2, ensure_ascii=False))
        # 将任务字典序列化为 JSON 并写入文件：
        # - indent=2：使用 2 空格缩进，便于人类阅读。
        # - ensure_ascii=False：允许非 ASCII 字符（如中文）原样输出，不转义。

    def create(self, subject: str, description: str = "") -> str:
        # create 方法：创建新任务。
        # 参数 subject: str —— 任务主题/标题。
        # 参数 description: str = "" —— 任务描述，默认为空。
        # 返回值 -> str —— 新任务的 JSON 字符串。
        task = {
            "id": self._next_id, "subject": subject, "description": description,
            "status": "pending", "blockedBy": [], "owner": "",
        }
        # 构建任务字典：
        # - "id": 自增 ID。
        # - "subject": 任务主题。
        # - "description": 任务描述。
        # - "status": "pending"（初始状态为待处理）。
        # - "blockedBy": []（初始无依赖）。
        # - "owner": ""（初始无负责人）。
        self._save(task)
        # 保存到文件。
        self._next_id += 1
        # ID 自增，为下一个任务做准备。
        return json.dumps(task, indent=2, ensure_ascii=False)
        # 返回格式化后的 JSON 字符串。

    def get(self, task_id: int) -> str:
        # get 方法：获取指定任务的详情。
        # 参数 task_id: int —— 任务 ID。
        # 返回值 -> str —— 任务的 JSON 字符串。
        return json.dumps(self._load(task_id), indent=2, ensure_ascii=False)
        # 加载任务并格式化为 JSON 字符串。

    def update(self, task_id: int, status: str = None,
               add_blocked_by: list = None, remove_blocked_by: list = None) -> str:
        # update 方法：更新任务状态或依赖关系。
        # 参数 task_id: int —— 任务 ID。
        # 参数 status: str = None —— 新状态，可选。
        # 参数 add_blocked_by: list = None —— 要添加的阻塞任务 ID 列表，可选。
        # 参数 remove_blocked_by: list = None —— 要移除的阻塞任务 ID 列表，可选。
        # 返回值 -> str —— 更新后的任务 JSON 字符串。
        task = self._load(task_id)
        # 加载任务。
        if status:
            # 如果提供了新状态。
            if status not in ("pending", "in_progress", "completed"):
                # 检查状态是否有效。
                raise ValueError(f"Invalid status: {status}")
                # 无效则抛出错误。
            task["status"] = status
            # 更新状态。
            if status == "completed":
                # 如果状态更新为已完成。
                self._clear_dependency(task_id)
                # 清除该任务对其他任务的阻塞关系。
        if add_blocked_by:
            # 如果提供了要添加的阻塞任务。
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
            # 合并现有阻塞列表和新列表，使用 set 去重。
        if remove_blocked_by:
            # 如果提供了要移除的阻塞任务。
            task["blockedBy"] = [x for x in task["blockedBy"] if x not in remove_blocked_by]
            # 过滤掉要移除的任务 ID。
        self._save(task)
        # 保存更新后的任务。
        return json.dumps(task, indent=2, ensure_ascii=False)
        # 返回更新后的 JSON 字符串。

    def _clear_dependency(self, completed_id: int):
        # _clear_dependency 方法：当任务完成时，从所有其他任务的阻塞列表中移除它。
        # 参数 completed_id: int —— 已完成任务的 ID。
        for f in self.dir.glob("task_*.json"):
            # 遍历所有任务文件。
            task = json.loads(f.read_text())
            # 读取任务。
            if completed_id in task.get("blockedBy", []):
                # 检查该任务是否被已完成任务阻塞。
                task["blockedBy"].remove(completed_id)
                # 从阻塞列表中移除已完成任务。
                self._save(task)
                # 保存更新。

    def list_all(self) -> str:
        # list_all 方法：列出所有任务。
        # 返回值 -> str —— 格式化的任务列表字符串。
        tasks = []
        # 任务列表。
        files = sorted(
            self.dir.glob("task_*.json"),
            key=lambda f: int(f.stem.split("_")[1])
        )
        # 按 ID 排序的任务文件列表。
        for f in files:
            tasks.append(json.loads(f.read_text()))
            # 读取并解析每个任务。
        if not tasks:
            # 如果没有任务。
            return "No tasks."
            # 返回提示。
        lines = []
        # 输出行列表。
        for t in tasks:
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            # 根据状态选择标记。
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            # 如果有阻塞依赖，显示阻塞信息。
            lines.append(f"{marker} #{t['id']}: {t['subject']}{blocked}")
            # 格式化输出行。
        return "\n".join(lines)
        # 返回格式化的任务列表。


TASKS = TaskManager(TASKS_DIR)
# 创建全局 TaskManager 实例。


# -- Base tool implementations --
# -- 基础工具实现 --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
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
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
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
    # 工具调度映射表，增加了任务管理工具。
    "bash":        lambda **kw: run_bash(kw["command"]),
    "read_file":   lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file":  lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":   lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    # task_create 工具：创建新任务。
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("addBlockedBy"), kw.get("removeBlockedBy")),
    # task_update 工具：更新任务状态或依赖。
    "task_list":   lambda **kw: TASKS.list_all(),
    # task_list 工具：列出所有任务。
    "task_get":    lambda **kw: TASKS.get(kw["task_id"]),
    # task_get 工具：获取任务详情。
}

TOOLS = [
    # 可用工具列表，增加了任务管理工具。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "task_create", "description": "Create a new task.",
     "input_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "description": {"type": "string"}}, "required": ["subject"]}},
    # task_create 工具定义：
    # - "subject": 任务主题，必填。
    # - "description": 任务描述，可选。
    {"name": "task_update", "description": "Update a task's status or dependencies.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}, "addBlockedBy": {"type": "array", "items": {"type": "integer"}}, "removeBlockedBy": {"type": "array", "items": {"type": "integer"}}}, "required": ["task_id"]}},
    # task_update 工具定义：
    # - "task_id": 任务 ID，必填。
    # - "status": 新状态，枚举值。
    # - "addBlockedBy": 要添加的阻塞任务 ID 数组。
    # - "removeBlockedBy": 要移除的阻塞任务 ID 数组。
    {"name": "task_list", "description": "List all tasks with status summary.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "task_get", "description": "Get full details of a task by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):
    # Agent 核心循环。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})
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
            query = input("\033[36ms07 >> \033[0m")
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