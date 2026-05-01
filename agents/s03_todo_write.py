#!/usr/bin/env python3
# 指定解释器为 python3。

# Harness: planning -- keeping the model on course without scripting the route.
# Harness：规划 —— 让模型保持正轨，而不需要人为编写具体路线。
# 本文件引入了 TodoManager，让模型能够自我跟踪任务进度，
# 并通过"唠叨提醒"（nag reminder）机制在模型忘记更新时强制提醒。

"""
s03_todo_write.py - TodoWrite

s03_todo_write.py - 待办事项追踪

The model tracks its own progress via a TodoManager. A nag reminder
forces it to keep updating when it forgets.

模型通过 TodoManager 跟踪自己的进度。当模型忘记更新时，
"唠叨提醒"机制会强制它继续更新待办事项。

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> | Tools   |
    |  prompt  |      |       |      | + todo  |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                                |
                    +-----------+-----------+
                    | TodoManager state     |
                    | [ ] task A            |
                    | [>] task B <- doing   |
                    | [x] task C            |
                    +-----------------------+
                                |
                    if rounds_since_todo >= 3:
                      inject <reminder>

Key insight: "The agent can track its own progress -- and I can see it."

核心洞察："智能体可以自己跟踪进度 —— 而且我能看到它。"
"""

import os
# 导入 os 模块：用于读取环境变量、操作路径等。

import subprocess
# 导入 subprocess 模块：用于执行外部 shell 命令。

from pathlib import Path
# 从 pathlib 导入 Path 类：用于现代路径操作。

from anthropic import Anthropic
# 从 anthropic 导入 Anthropic 类：Anthropic SDK，与 Claude API 通信。

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv：加载 .env 文件中的环境变量。

load_dotenv(override=True)
# 加载环境变量，override=True 覆盖已存在的变量。

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查是否设置了自定义 API 基础 URL。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 如果设置了自定义 base URL，移除认证令牌。

WORKDIR = Path.cwd()
# 设置工作目录为当前目录。

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端。

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取模型 ID（必需配置）。

SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use the todo tool to plan multi-step tasks. Mark in_progress before starting, completed when done.
Prefer tools over prose."""
# 系统提示词：设定模型为编程助手，指示使用 todo 工具规划多步骤任务。
# - "Mark in_progress before starting"：开始任务前标记为进行中。
# - "completed when done"：完成后标记为已完成。
# - "Prefer tools over prose"：优先使用工具而非散文（即用行动代替长篇解释）。


# -- TodoManager: structured state the LLM writes to --
# -- TodoManager：LLM 写入的结构化状态 —— 待办事项管理器 --
class TodoManager:
    # 定义 TodoManager 类：管理待办事项列表，提供验证、更新和渲染功能。
    # 这个类维护一个待办事项列表，模型可以通过 todo 工具来更新它。
    def __init__(self):
        # 构造函数：初始化 TodoManager 实例。
        self.items = []
        # self.items：存储待办事项的内部列表。
        # 每个待办事项是一个字典，包含 id、text（文本）、status（状态）等字段。

    def update(self, items: list) -> str:
        # 定义 update 方法：更新待办事项列表，进行验证后保存。
        # 参数 items: list —— 模型传入的新待办事项列表，每个元素是一个字典。
        # 返回值 -> str —— 返回渲染后的待办事项列表字符串。
        if len(items) > 20:
            # 检查待办事项数量是否超过 20 个。
            raise ValueError("Max 20 todos allowed")
            # 超过限制则抛出 ValueError，防止列表过长影响模型注意力。
        validated = []
        # validated：经过验证的待办事项列表，存储验证通过的项。
        in_progress_count = 0
        # in_progress_count：统计状态为 "in_progress"（进行中）的事项数量。
        # 用于确保同时只有一个任务处于进行中状态。
        for i, item in enumerate(items):
            # 遍历每个待办事项，enumerate 同时返回索引 i 和元素 item。
            text = str(item.get("text", "")).strip()
            # 提取待办事项文本：
            # - item.get("text", "")：从字典中获取 text 字段，如果不存在则返回空字符串。
            # - str(...)：确保转换为字符串类型（防止传入非字符串值）。
            # - .strip()：去除首尾空白字符。
            status = str(item.get("status", "pending")).lower()
            # 提取状态字段：
            # - item.get("status", "pending")：默认状态为 "pending"（待处理）。
            # - str(...).lower()：转换为小写，实现大小写不敏感。
            item_id = str(item.get("id", str(i + 1)))
            # 提取 ID 字段：
            # - item.get("id", str(i + 1))：如果没有提供 id，使用索引 + 1 作为默认 ID。
            # - str(...)：确保 ID 为字符串类型。
            if not text:
                # 检查文本是否为空（去除空白后）。
                raise ValueError(f"Item {item_id}: text required")
                # 文本为空则抛出错误，要求每个待办事项必须有文本描述。
            if status not in ("pending", "in_progress", "completed"):
                # 检查状态是否为允许的值之一。
                raise ValueError(f"Item {item_id}: invalid status '{status}'")
                # 状态无效则抛出错误。
            if status == "in_progress":
                # 如果状态为进行中。
                in_progress_count += 1
                # 增加进行中计数。
            validated.append({"id": item_id, "text": text, "status": status})
            # 将验证通过的待办事项添加到 validated 列表。
        if in_progress_count > 1:
            # 检查进行中事项的数量是否超过 1。
            raise ValueError("Only one task can be in_progress at a time")
            # 同时只能有一个任务进行中，防止模型在多个任务间分散注意力。
        self.items = validated
        # 将验证后的列表保存为当前待办事项。
        return self.render()
        # 返回渲染后的待办事项列表字符串。

    def render(self) -> str:
        # 定义 render 方法：将待办事项列表渲染为可读的字符串格式。
        # 返回值 -> str —— 格式化后的待办事项列表字符串。
        if not self.items:
            # 检查待办事项列表是否为空。
            return "No todos."
            # 空列表返回提示信息。
        lines = []
        # lines：存储渲染后的每一行文本。
        for item in self.items:
            # 遍历每个待办事项。
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            # 根据状态选择对应的标记符号：
            # - "pending"（待处理）：[ ] 空方框。
            # - "in_progress"（进行中）：[>] 箭头，表示正在做。
            # - "completed"（已完成）：[x] 打叉，表示已完成。
            # 使用字典映射实现状态到标记的转换。
            lines.append(f"{marker} #{item['id']}: {item['text']}")
            # 格式化每一行：标记符号 + ID + 文本描述。
            # f-string 格式化字符串。
        done = sum(1 for t in self.items if t["status"] == "completed")
        # 统计已完成的事项数量：
        # - 生成器表达式：遍历所有事项，status == "completed" 为 True 时产生 1。
        # - sum() 对所有 1 求和，得到已完成数量。
        lines.append(f"\n({done}/{len(self.items)} completed)")
        # 添加完成进度统计行：已完成数 / 总数。
        return "\n".join(lines)
        # 将所有行用换行符连接成完整字符串返回。


TODO = TodoManager()
# 创建全局 TodoManager 实例：
# TODO 是全局单例，所有工具调用共享同一个待办事项管理器。
# 这样模型的 todo 工具操作会影响同一个状态。


# -- Tool implementations --
# -- 工具实现 --
def safe_path(p: str) -> Path:
    # 安全路径检查函数，与 s02 中相同。
    # 参数 p: str —— 用户提供的相对路径。
    # 返回值 -> Path —— 经过安全检查的绝对路径。
    path = (WORKDIR / p).resolve()
    # 拼接工作目录和用户路径，解析为绝对路径。
    if not path.is_relative_to(WORKDIR):
        # 检查路径是否在工作目录内。
        raise ValueError(f"Path escapes workspace: {p}")
        # 路径逃逸则抛出错误。
    return path
    # 返回安全路径。

def run_bash(command: str) -> str:
    # 执行 bash 命令，与 s02 中相同。
    # 参数 command: str —— 要执行的命令。
    # 返回值 -> str —— 命令输出。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 危险命令黑名单。
    if any(d in command for d in dangerous):
        # 检查是否包含危险命令。
        return "Error: Dangerous command blocked"
        # 拒绝执行危险命令。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # 执行命令，捕获输出，120秒超时。
        out = (r.stdout + r.stderr).strip()
        # 合并 stdout 和 stderr，去除空白。
        return out[:50000] if out else "(no output)"
        # 返回输出（限制50000字符）。
    except subprocess.TimeoutExpired:
        # 捕获超时异常。
        return "Error: Timeout (120s)"
        # 返回超时错误。

def run_read(path: str, limit: int = None) -> str:
    # 读取文件内容，与 s02 中相同。
    # 参数 path: str —— 文件路径。
    # 参数 limit: int = None —— 可选的行数限制。
    # 返回值 -> str —— 文件内容。
    try:
        lines = safe_path(path).read_text().splitlines()
        # 安全读取文件并按行分割。
        if limit and limit < len(lines):
            # 如果设置了行数限制且超出。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
            # 截取前 limit 行并添加省略提示。
        return "\n".join(lines)[:50000]
        # 返回内容（限制50000字符）。
    except Exception as e:
        # 捕获异常。
        return f"Error: {e}"
        # 返回错误信息。

def run_write(path: str, content: str) -> str:
    # 写入文件，与 s02 中相同。
    # 参数 path: str —— 文件路径。
    # 参数 content: str —— 文件内容。
    # 返回值 -> str —— 操作结果。
    try:
        fp = safe_path(path)
        # 获取安全路径。
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 创建父目录（如果不存在）。
        fp.write_text(content)
        # 写入内容。
        return f"Wrote {len(content)} bytes"
        # 返回成功信息。
    except Exception as e:
        # 捕获异常。
        return f"Error: {e}"
        # 返回错误信息。

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件，与 s02 中相同。
    # 参数 path: str —— 文件路径。
    # 参数 old_text: str —— 要替换的文本。
    # 参数 new_text: str —— 新文本。
    # 返回值 -> str —— 操作结果。
    try:
        fp = safe_path(path)
        # 获取安全路径。
        content = fp.read_text()
        # 读取文件内容。
        if old_text not in content:
            # 检查原始文本是否存在。
            return f"Error: Text not found in {path}"
            # 不存在则返回错误。
        fp.write_text(content.replace(old_text, new_text, 1))
        # 替换文本（只替换第一次出现）。
        return f"Edited {path}"
        # 返回成功信息。
    except Exception as e:
        # 捕获异常。
        return f"Error: {e}"
        # 返回错误信息。


TOOL_HANDLERS = {
    # 工具调度映射表，在 s02 基础上增加了 todo 工具。
    "bash":       lambda **kw: run_bash(kw["command"]),
    # bash 工具：执行 shell 命令。
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    # read_file 工具：读取文件。
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    # write_file 工具：写入文件。
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # edit_file 工具：编辑文件。
    "todo":       lambda **kw: TODO.update(kw["items"]),
    # todo 工具：更新待办事项列表。
    # - kw["items"]：模型传入的待办事项列表。
    # - TODO.update()：调用 TodoManager 的 update 方法进行验证和保存。
}

TOOLS = [
    # 可用工具列表，在 s02 基础上增加了 todo 工具。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # bash 工具定义。
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # read_file 工具定义。
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # write_file 工具定义。
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # edit_file 工具定义。
    {"name": "todo", "description": "Update task list. Track progress on multi-step tasks.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}}, "required": ["id", "text", "status"]}}, "required": ["items"]}},
    # todo 工具定义：
    # - "description": 描述为"更新任务列表，追踪多步骤任务进度"。
    # - "input_schema": 输入参数结构。
    #   - "items": 待办事项数组。
    #     - 每个元素是对象，包含 id（字符串）、text（字符串）、status（字符串，枚举值）。
    #     - status 的 enum 限制了只能是 "pending"、"in_progress"、"completed" 三种状态。
    #   - "required": ["items"]：items 是必填参数。
]


# -- Agent loop with nag reminder injection --
# -- 带有唠叨提醒注入的 Agent 循环 --
def agent_loop(messages: list):
    # 定义 agent_loop 函数：核心 Agent 循环，增加了待办事项提醒机制。
    # 参数 messages: list —— 消息历史列表。
    rounds_since_todo = 0
    # rounds_since_todo：记录自上次使用 todo 工具以来经过的对话轮数。
    # 用于判断是否需要注入"唠叨提醒"。
    while True:
        # 无限循环。
        # Nag reminder is injected below, alongside tool results
        # 唠叨提醒会在下方与工具结果一起注入（如果需要的话）。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 调用 Claude API 获取模型回复。
        messages.append({"role": "assistant", "content": response.content})
        # 追加模型回复到消息历史。
        if response.stop_reason != "tool_use":
            # 如果模型没有调用工具。
            return
            # 结束循环。
        results = []
        # 初始化结果列表。
        used_todo = False
        # used_todo：标记本轮是否使用了 todo 工具。
        # 如果使用了，重置 rounds_since_todo 计数器。
        for block in response.content:
            # 遍历模型回复中的每个内容块。
            if block.type == "tool_use":
                # 检查是否为工具调用。
                handler = TOOL_HANDLERS.get(block.name)
                # 查找对应工具的处理函数。
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    # 调用处理函数执行工具。
                except Exception as e:
                    # 捕获执行过程中的异常。
                    output = f"Error: {e}"
                    # 返回错误信息。
                print(f"> {block.name}:")
                # 打印工具名称。
                print(str(output)[:200])
                # 打印输出前200字符。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                # 追加工具结果。
                if block.name == "todo":
                    # 检查是否使用了 todo 工具。
                    used_todo = True
                    # 标记本轮已使用 todo 工具。
        rounds_since_todo = 0 if used_todo else rounds_since_todo + 1
        # 更新计数器：
        # - 如果使用了 todo 工具，重置为 0。
        # - 否则，计数器 + 1。
        if rounds_since_todo >= 3:
            # 如果连续 3 轮没有使用 todo 工具。
            results.append({"type": "text", "text": "<reminder>Update your todos.</reminder>"})
            # 注入唠叨提醒消息，以文本块的形式追加到结果中。
            # 模型在下一轮会看到这条提醒，促使其更新待办事项。
        messages.append({"role": "user", "content": results})
        # 将工具执行结果和可能的提醒追加到消息历史。


if __name__ == "__main__":
    # 当脚本直接运行时执行。
    history = []
    # 初始化空的消息历史。
    while True:
        # 无限循环接收用户输入。
        try:
            query = input("\033[36ms03 >> \033[0m")
            # 显示青色提示符 "s03 >> "，接收用户输入。
        except (EOFError, KeyboardInterrupt):
            # 捕获 EOF 或中断。
            break
            # 退出循环。
        if query.strip().lower() in ("q", "exit", ""):
            # 检查退出命令。
            break
            # 退出循环。
        history.append({"role": "user", "content": query})
        # 追加用户输入到历史。
        agent_loop(history)
        # 启动 Agent 循环。
        response_content = history[-1]["content"]
        # 获取最后一条消息内容。
        if isinstance(response_content, list):
            # 检查是否为列表。
            for block in response_content:
                # 遍历每个块。
                if hasattr(block, "text"):
                    # 检查是否有 text 属性。
                    print(block.text)
                    # 打印文本。
        print()
        # 打印空行。
