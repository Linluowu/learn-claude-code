#!/usr/bin/env python3
# 指定解释器为 python3。

# Harness: context isolation -- protecting the model's clarity of thought.
# Harness：上下文隔离 —— 保护模型思考的清晰度。
# 本文件引入子智能体（Subagent）概念，允许主智能体派生子智能体处理子任务，
# 子智能体在独立的上下文中工作，只将摘要返回给父智能体，保持父上下文干净。

"""
s04_subagent.py - Subagents

s04_subagent.py - 子智能体

Spawn a child agent with fresh messages=[]. The child works in its own
context, sharing the filesystem, then returns only a summary to the parent.

创建一个子智能体，使用全新的 messages=[]（空消息历史）。
子智能体在自己的上下文中工作，与父智能体共享文件系统，
最后只将一个摘要返回给父智能体。

    Parent agent                     Subagent
    +------------------+             +------------------+
    | messages=[...]   |             | messages=[]      |  <-- fresh
    |                  |  dispatch   |                  |
    | tool: task       | ---------->| while tool_use:  |
    |   prompt="..."   |            |   call tools     |
    |   description="" |            |   append results |
    |                  |  summary   |                  |
    |   result = "..." | <--------- | return last text |
    +------------------+             +------------------+
              |
    Parent context stays clean.
    Subagent context is discarded.

Key insight: "Process isolation gives context isolation for free."

核心洞察："进程隔离免费附带了上下文隔离。"
通过创建新的消息历史（空列表），子智能体天然就拥有了独立的上下文，
不需要额外的机制来隔离。这是利用进程（函数调用）边界自然实现隔离的优雅设计。
"""

import os
# 导入 os 模块：用于读取环境变量。

import subprocess
# 导入 subprocess 模块：用于执行外部命令。

from pathlib import Path
# 从 pathlib 导入 Path：用于现代路径操作。

from anthropic import Anthropic
# 从 anthropic 导入 Anthropic：Anthropic SDK，与 Claude API 通信。

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv：加载 .env 文件。

load_dotenv(override=True)
# 加载环境变量。

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查是否设置了自定义 API 基础 URL。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 设置了则移除认证令牌。

WORKDIR = Path.cwd()
# 设置工作目录为当前目录。

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端。

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取模型 ID。

SYSTEM = f"You are a coding agent at {WORKDIR}. Use the task tool to delegate exploration or subtasks."
# 主智能体系统提示词：告知当前工作目录，指示使用 task 工具委派探索任务或子任务。

SUBAGENT_SYSTEM = f"You are a coding subagent at {WORKDIR}. Complete the given task, then summarize your findings."
# 子智能体系统提示词：告知当前工作目录，指示完成给定任务后总结发现。
# 子智能体与主智能体的角色不同：子智能体专注于完成具体任务并返回摘要。


# -- Tool implementations shared by parent and child --
# -- 父智能体和子智能体共享的工具实现 --
def safe_path(p: str) -> Path:
    # 安全路径检查函数。
    # 参数 p: str —— 用户提供的相对路径。
    # 返回值 -> Path —— 经过安全检查的绝对路径。
    path = (WORKDIR / p).resolve()
    # 拼接工作目录和用户提供的路径，解析为绝对路径。
    if not path.is_relative_to(WORKDIR):
        # 检查路径是否逃逸出工作目录。
        raise ValueError(f"Path escapes workspace: {p}")
        # 路径不安全则抛出错误。
    return path
    # 返回安全路径。

def run_bash(command: str) -> str:
    # 执行 bash 命令。
    # 参数 command: str —— 要执行的命令。
    # 返回值 -> str —— 命令输出。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 危险命令黑名单。
    if any(d in command for d in dangerous):
        # 检查是否包含危险命令。
        return "Error: Dangerous command blocked"
        # 拒绝执行。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        # 执行命令，120秒超时。
        out = (r.stdout + r.stderr).strip()
        # 合并 stdout 和 stderr，去除空白。
        return out[:50000] if out else "(no output)"
        # 返回输出（限制50000字符）。
    except subprocess.TimeoutExpired:
        # 捕获超时异常。
        return "Error: Timeout (120s)"
        # 返回超时错误。
    except (FileNotFoundError, OSError) as e:
        # 捕获文件未找到或操作系统错误。
        return f"Error: {e}"
        # 返回错误信息。

def run_read(path: str, limit: int = None) -> str:
    # 读取文件内容。
    # 参数 path: str —— 文件路径。
    # 参数 limit: int = None —— 可选行数限制。
    # 返回值 -> str —— 文件内容。
    try:
        lines = safe_path(path).read_text().splitlines()
        # 安全读取文件并按行分割。
        if limit and limit < len(lines):
            # 如果超出限制。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
            # 截取并添加省略提示。
        return "\n".join(lines)[:50000]
        # 返回内容（限制50000字符）。
    except Exception as e:
        # 捕获异常。
        return f"Error: {e}"
        # 返回错误信息。

def run_write(path: str, content: str) -> str:
    # 写入文件。
    # 参数 path: str —— 文件路径。
    # 参数 content: str —— 文件内容。
    # 返回值 -> str —— 操作结果。
    try:
        fp = safe_path(path)
        # 获取安全路径。
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 创建父目录。
        fp.write_text(content)
        # 写入内容。
        return f"Wrote {len(content)} bytes"
        # 返回成功信息。
    except Exception as e:
        # 捕获异常。
        return f"Error: {e}"
        # 返回错误信息。

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件。
    # 参数 path: str —— 文件路径。
    # 参数 old_text: str —— 要替换的文本。
    # 参数 new_text: str —— 新文本。
    # 返回值 -> str —— 操作结果。
    try:
        fp = safe_path(path)
        # 获取安全路径。
        content = fp.read_text()
        # 读取内容。
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
    # 工具调度映射表。
    "bash":       lambda **kw: run_bash(kw["command"]),
    # bash 工具。
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    # read_file 工具。
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    # write_file 工具。
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # edit_file 工具。
}

# Child gets all base tools except task (no recursive spawning)
# 子智能体获得所有基础工具，但不包括 task 工具（禁止递归创建子智能体）。
CHILD_TOOLS = [
    # 子智能体可用的工具列表。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # bash 工具。
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # read_file 工具。
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # write_file 工具。
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # edit_file 工具。
]
# 注意：CHILD_TOOLS 不包含 task 工具，防止子智能体再创建孙智能体，避免无限递归。


# -- Subagent: fresh context, filtered tools, summary-only return --
# -- 子智能体：全新上下文、过滤后的工具、仅返回摘要 --
def run_subagent(prompt: str) -> str:
    # 定义 run_subagent 函数：创建并运行子智能体，处理子任务。
    # 参数 prompt: str —— 子智能体的任务提示词，描述需要完成的任务。
    # 返回值 -> str —— 子智能体完成任务的摘要/总结。
    sub_messages = [{"role": "user", "content": prompt}]
    # 子智能体的消息历史：从空列表开始，只包含一条用户消息（即任务提示词）。
    # 这是"上下文隔离"的关键 —— 子智能体看不到父智能体的对话历史，
    # 只能看到分配给自己的任务。
    for _ in range(30):
        # 最多循环 30 轮（安全限制）：
        # 防止子智能体陷入无限循环，保护 API 调用配额。
        # 使用 _ 作为循环变量，表示这个变量在循环体内不被使用。
        response = client.messages.create(
            model=MODEL, system=SUBAGENT_SYSTEM, messages=sub_messages,
            tools=CHILD_TOOLS, max_tokens=8000,
        )
        # 调用 Claude API 为子智能体生成回复：
        # - model=MODEL：使用与主智能体相同的模型。
        # - system=SUBAGENT_SYSTEM：使用子智能体专用的系统提示词。
        # - messages=sub_messages：传入子智能体独立的消息历史。
        # - tools=CHILD_TOOLS：只给子智能体基础工具，不给 task 工具。
        # - max_tokens=8000：限制单次回复长度。
        sub_messages.append({"role": "assistant", "content": response.content})
        # 将子智能体的回复追加到其消息历史中。
        if response.stop_reason != "tool_use":
            # 如果子智能体没有调用工具，说明任务已完成或已给出最终回答。
            break
            # 跳出循环，结束子智能体的运行。
        results = []
        # 初始化结果列表。
        for block in response.content:
            # 遍历子智能体回复中的每个内容块。
            if block.type == "tool_use":
                # 检查是否为工具调用。
                handler = TOOL_HANDLERS.get(block.name)
                # 查找对应工具的处理函数。
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                # 执行工具调用。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)[:50000]})
                # 构建工具结果，限制输出长度为 50000 字符。
        sub_messages.append({"role": "user", "content": results})
        # 将工具执行结果追加到子智能体的消息历史中。
    # Only the final text returns to the parent -- child context is discarded
    # 只有最终的文本内容会返回给父智能体 —— 子智能体的上下文会被丢弃。
    # 这是"上下文隔离"的另一半：不仅创建时隔离（空消息历史），
    # 销毁时也隔离（不保留任何中间状态）。
    return "".join(b.text for b in response.content if hasattr(b, "text")) or "(no summary)"
    # 从最后的模型回复中提取所有文本块，拼接成完整摘要返回：
    # - b.text for b in response.content：遍历所有内容块，获取文本内容。
    # - if hasattr(b, "text")：过滤出文本块（跳过工具调用块）。
    # - "".join(...)：将所有文本拼接成一个字符串。
    # - or "(no summary)"：如果没有任何文本，返回默认提示。


# -- Parent tools: base tools + task dispatcher --
# -- 父智能体工具：基础工具 + 任务调度器 --
PARENT_TOOLS = CHILD_TOOLS + [
    # 父智能体的工具列表：在子智能体工具基础上增加 task 工具。
    {"name": "task", "description": "Spawn a subagent with fresh context. It shares the filesystem but not conversation history.",
     "input_schema": {"type": "object", "properties": {"prompt": {"type": "string"}, "description": {"type": "string", "description": "Short description of the task"}}, "required": ["prompt"]}},
    # task 工具定义：
    # - "name": "task"：工具名称。
    # - "description": 描述为"创建子智能体，使用全新上下文，共享文件系统但不共享对话历史"。
    # - "input_schema": 输入参数结构。
    #   - "prompt": 子智能体的任务提示词，必填。
    #   - "description": 任务的简短描述，可选（用于日志显示）。
    #   - "required": ["prompt"]：prompt 是必填参数。
]


def agent_loop(messages: list):
    # 定义 agent_loop 函数：主智能体的核心循环。
    # 参数 messages: list —— 消息历史列表。
    while True:
        # 无限循环。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=PARENT_TOOLS, max_tokens=8000,
        )
        # 调用 Claude API，使用 PARENT_TOOLS（包含 task 工具）。
        messages.append({"role": "assistant", "content": response.content})
        # 追加模型回复。
        if response.stop_reason != "tool_use":
            # 如果没有调用工具。
            return
            # 结束循环。
        results = []
        # 初始化结果列表。
        for block in response.content:
            # 遍历内容块。
            if block.type == "tool_use":
                # 检查是否为工具调用。
                if block.name == "task":
                    # 如果工具是 task（创建子智能体）。
                    desc = block.input.get("description", "subtask")
                    # 获取任务描述，默认值为 "subtask"（用于日志显示）。
                    prompt = block.input.get("prompt", "")
                    # 获取任务提示词（子智能体将接收的任务描述）。
                    print(f"> task ({desc}): {prompt[:80]}")
                    # 打印任务信息到终端，显示描述和前80字符的提示词。
                    output = run_subagent(prompt)
                    # 调用 run_subagent() 创建子智能体并执行子任务，等待子智能体完成。
                else:
                    # 如果不是 task 工具，使用常规工具调度。
                    handler = TOOL_HANDLERS.get(block.name)
                    # 查找处理函数。
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    # 执行工具调用。
                print(f"  {str(output)[:200]}")
                # 打印工具输出前200字符（带两个空格缩进，表示子输出）。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                # 构建工具结果。
        messages.append({"role": "user", "content": results})
        # 追加结果到消息历史。


if __name__ == "__main__":
    # 当脚本直接运行时执行。
    history = []
    # 初始化空的消息历史。
    while True:
        # 无限循环。
        try:
            query = input("\033[36ms04 >> \033[0m")
            # 显示青色提示符 "s04 >> "，接收用户输入。
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