#!/usr/bin/env python3
# 指定解释器为 python3。

# Harness: compression -- clean memory for infinite sessions.
# Harness：压缩 —— 为无限会话提供干净的内存。
# 本文件引入三层压缩管道，让 Agent 能够处理无限长的对话而不受上下文长度限制。

"""
s06_context_compact.py - Compact

s06_context_compact.py - 上下文压缩

Three-layer compression pipeline so the agent can work forever:

三层压缩管道，让智能体能够永远工作：

    Every turn:
    +------------------+
    | Tool call result |
    +------------------+
            |
            v
    [Layer 1: micro_compact]        (silent, every turn)
      Replace non-read_file tool_result content older than last 3
      with "[Previous: used {tool_name}]"
            |
            v
    [Check: tokens > 50000?]
       |               |
       no              yes
       |               |
       v               v
    continue    [Layer 2: auto_compact]
                  Save full transcript to .transcripts/
                  Ask LLM to summarize conversation.
                  Replace all messages with [summary].
                        |
                        v
                [Layer 3: compact tool]
                  Model calls compact -> immediate summarization.
                  Same as auto, triggered manually.

Key insight: "The agent can forget strategically and keep working forever."

核心洞察："智能体可以有策略地遗忘，并永远工作下去。"
"""

import json
# 导入 json 模块：用于序列化和反序列化 JSON 数据。

import os
# 导入 os 模块。

import subprocess
# 导入 subprocess 模块。

import time
# 导入 time 模块：用于生成时间戳。

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

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."
# 系统提示词。

THRESHOLD = 50000
# THRESHOLD：自动压缩的 token 阈值。
# 当估计的 token 数超过此值时，触发第二层自动压缩。

TRANSCRIPT_DIR = WORKDIR / ".transcripts"
# TRANSCRIPT_DIR：完整对话记录的保存目录。
# 压缩前会将完整对话保存到此目录，以便后续查阅。

KEEP_RECENT = 3
# KEEP_RECENT：微压缩时保留的最近工具结果数量。
# 只保留最近 3 个工具结果的完整内容，更早的结果会被替换为占位符。

PRESERVE_RESULT_TOOLS = {"read_file"}
# PRESERVE_RESULT_TOOLS：在微压缩中保留完整内容的工具集合。
# read_file 的结果通常是参考材料，压缩后会导致模型反复重新读取文件。
# 因此保留 read_file 的完整输出，避免重复读取。


def estimate_tokens(messages: list) -> int:
    # estimate_tokens 函数：粗略估计消息列表的 token 数量。
    # 参数 messages: list —— 消息历史列表。
    # 返回值 -> int —— 估计的 token 数。
    # 这是一个简单的启发式估计：假设平均每个 token 约 4 个字符。
    return len(str(messages)) // 4
    # 将消息列表转为字符串，计算长度，除以 4 得到粗略的 token 估计。
    # // 4：整数除法，向下取整。
    # 实际 token 数取决于 tokenizer，这个估计不精确但足够用于阈值判断。


# -- Layer 1: micro_compact - replace old tool results with placeholders --
# -- 第一层：微压缩 —— 将旧的工具结果替换为占位符 --
def micro_compact(messages: list) -> list:
    # micro_compact 函数：微压缩，将旧的工具结果替换为简短占位符。
    # 参数 messages: list —— 消息历史列表。
    # 返回值 -> list —— 压缩后的消息列表（原地修改后返回）。
    # 收集 (msg_index, part_index, tool_result_dict) for all tool_result entries
    # 收集所有 tool_result 条目的位置信息。
    tool_results = []
    # tool_results：存储所有工具结果的元组列表。
    # 每个元组为 (消息索引, 部分索引, 结果字典)。
    for msg_idx, msg in enumerate(messages):
        # 遍历消息列表，enumerate 返回索引和消息。
        if msg["role"] == "user" and isinstance(msg.get("content"), list):
            # 检查消息角色是否为 user，且内容是否为列表。
            # 工具结果以用户消息的形式存在，内容是列表（包含多个工具结果块）。
            for part_idx, part in enumerate(msg["content"]):
                # 遍历消息内容列表中的每个部分。
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    # 检查部分是否为字典且类型为 tool_result。
                    tool_results.append((msg_idx, part_idx, part))
                    # 将位置信息和结果字典添加到列表。
    if len(tool_results) <= KEEP_RECENT:
        # 如果工具结果总数不超过保留数量。
        return messages
        # 不需要压缩，直接返回原列表。
    # Find tool_name for each result by matching tool_use_id in prior assistant messages
    # 通过在之前的助手消息中匹配 tool_use_id 来查找每个结果对应的工具名称。
    tool_name_map = {}
    # tool_name_map：字典，映射 tool_use_id -> 工具名称。
    for msg in messages:
        # 遍历所有消息。
        if msg["role"] == "assistant":
            # 只检查助手消息（工具调用请求在助手消息中）。
            content = msg.get("content", [])
            # 获取消息内容。
            if isinstance(content, list):
                # 如果内容是列表。
                for block in content:
                    # 遍历每个内容块。
                    if hasattr(block, "type") and block.type == "tool_use":
                        # 检查是否为 tool_use 块。
                        tool_name_map[block.id] = block.name
                        # 将工具调用 ID 映射到工具名称。
    # Clear old results (keep last KEEP_RECENT). Preserve read_file outputs because
    # they are reference material; compacting them forces the agent to re-read files.
    # 清除旧结果（保留最近的 KEEP_RECENT 个）。保留 read_file 的输出，
    # 因为它们是参考材料；压缩它们会迫使智能体反复重新读取文件。
    to_clear = tool_results[:-KEEP_RECENT]
    # 获取需要被清除的旧结果：除最后 KEEP_RECENT 个之外的所有结果。
    # Python 切片 [:-KEEP_RECENT] 表示从开头到倒数第 KEEP_RECENT 个之前。
    for _, _, result in to_clear:
        # 遍历每个需要清除的结果。
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            # 如果内容不是字符串或长度不超过 100 字符。
            continue
            # 跳过，不压缩短内容（可能已经是占位符或重要短消息）。
        tool_id = result.get("tool_use_id", "")
        # 获取工具调用 ID。
        tool_name = tool_name_map.get(tool_id, "unknown")
        # 通过 ID 查找工具名称，找不到则使用 "unknown"。
        if tool_name in PRESERVE_RESULT_TOOLS:
            # 如果工具在保留列表中（如 read_file）。
            continue
            # 跳过，不压缩保留的工具结果。
        result["content"] = f"[Previous: used {tool_name}]"
        # 将工具结果内容替换为占位符，提示模型这是之前使用过的工具的结果。
    return messages
    # 返回压缩后的消息列表。


# -- Layer 2: auto_compact - save transcript, summarize, replace messages --
# -- 第二层：自动压缩 —— 保存对话记录、总结、替换消息 --
def auto_compact(messages: list) -> list:
    # auto_compact 函数：自动压缩，将完整对话保存并总结为摘要。
    # 参数 messages: list —— 消息历史列表。
    # 返回值 -> list —— 替换为摘要后的新消息列表。
    # Save full transcript to disk
    # 将完整对话记录保存到磁盘。
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    # 创建对话记录目录（如果不存在）。
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    # 生成对话记录文件路径，使用时间戳命名避免冲突。
    # time.time() 返回当前时间的秒数（浮点数），int() 转为整数。
    with open(transcript_path, "w") as f:
        # 以写入模式打开文件。
        for msg in messages:
            # 遍历每条消息。
            f.write(json.dumps(msg, default=str) + "\n")
            # 将消息序列化为 JSON 并写入文件，每条消息占一行（JSONL 格式）。
            # default=str：对于无法 JSON 序列化的对象，使用 str() 转换。
    print(f"[transcript saved: {transcript_path}]")
    # 打印保存信息。
    # Ask LLM to summarize
    # 请求 LLM 总结对话。
    conversation_text = json.dumps(messages, default=str)[-80000:]
    # 将消息列表序列化为 JSON，取最后 80000 个字符。
    # [-80000:]：切片取最后 80000 字符，限制输入长度避免超出 API 限制。
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity. Include: "
            "1) What was accomplished, 2) Current state, 3) Key decisions made. "
            "Be concise but preserve critical details.\n\n" + conversation_text}],
        max_tokens=2000,
    )
    # 调用 Claude API 生成对话摘要：
    # - 提示词要求总结对话的三个方面：已完成的内容、当前状态、关键决策。
    # - "Be concise but preserve critical details"：要求简洁但保留关键细节。
    # - 传入对话文本作为上下文。
    # - max_tokens=2000：限制摘要长度。
    summary = next((block.text for block in response.content if hasattr(block, "text")), "")
    # 从模型回复中提取第一个文本块的内容作为摘要。
    # next() 配合生成器表达式：遍历所有内容块，找到第一个有 text 属性的块。
    # 如果没有文本块，返回空字符串（第二个参数 ""</a>）。
    if not summary:
        # 如果摘要为空。
        summary = "No summary generated."
        # 使用默认提示。
    # Replace all messages with compressed summary
    # 将所有消息替换为压缩后的摘要。
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
    ]
    # 返回只包含一条消息的新列表：
    # - 消息包含压缩提示、对话记录文件路径和摘要内容。
    # 这样模型在后续对话中仍能通过摘要了解之前的上下文。


# -- Tool implementations --
# -- 工具实现 --
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
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    # 工具调度映射表，增加了 compact 工具。
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "compact":    lambda **kw: "Manual compression requested.",
    # compact 工具：触发手动压缩。
    # 返回提示信息，实际的压缩在 agent_loop 中处理。
}

TOOLS = [
    # 可用工具列表，增加了 compact 工具。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "compact", "description": "Trigger manual conversation compression.",
     "input_schema": {"type": "object", "properties": {"focus": {"type": "string", "description": "What to preserve in the summary"}}}},
    # compact 工具定义：
    # - "description": "触发手动对话压缩"。
    # - "input_schema": 输入参数。
    #   - "focus": 可选参数，描述希望在摘要中保留的重点内容。
]


def agent_loop(messages: list):
    # Agent 核心循环，增加了三层压缩机制。
    while True:
        # Layer 1: micro_compact before each LLM call
        # 第一层：在每次调用 LLM 之前进行微压缩。
        micro_compact(messages)
        # 执行微压缩，将旧的工具结果替换为占位符。
        # Layer 2: auto_compact if token estimate exceeds threshold
        # 第二层：如果 token 估计超过阈值，触发自动压缩。
        if estimate_tokens(messages) > THRESHOLD:
            # 检查估计 token 数是否超过阈值。
            print("[auto_compact triggered]")
            # 打印触发信息。
            messages[:] = auto_compact(messages)
            # 使用切片赋值 messages[:] 原地替换列表内容。
            # 这样保持 messages 对象的引用不变，但内容被替换为压缩后的摘要。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 调用 Claude API。
        messages.append({"role": "assistant", "content": response.content})
        # 追加模型回复。
        if response.stop_reason != "tool_use":
            # 如果没有调用工具。
            return
            # 结束循环。
        results = []
        # 初始化结果列表。
        manual_compact = False
        # manual_compact：标记是否触发了手动压缩。
        for block in response.content:
            # 遍历内容块。
            if block.type == "tool_use":
                # 检查是否为工具调用。
                if block.name == "compact":
                    # 如果工具是 compact（手动压缩）。
                    manual_compact = True
                    # 标记手动压缩已触发。
                    output = "Compressing..."
                    # 设置输出提示。
                else:
                    # 其他工具。
                    handler = TOOL_HANDLERS.get(block.name)
                    # 查找处理函数。
                    try:
                        output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                        # 执行工具调用。
                    except Exception as e:
                        # 捕获异常。
                        output = f"Error: {e}"
                        # 返回错误信息。
                print(f"> {block.name}:")
                # 打印工具名称。
                print(str(output)[:200])
                # 打印输出。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(output)})
                # 追加工具结果。
        messages.append({"role": "user", "content": results})
        # 追加结果到消息历史。
        # Layer 3: manual compact triggered by the compact tool
        # 第三层：由 compact 工具触发的手动压缩。
        if manual_compact:
            # 如果触发了手动压缩。
            print("[manual compact]")
            # 打印信息。
            messages[:] = auto_compact(messages)
            # 执行自动压缩，替换消息历史为摘要。
            return
            # 结束当前 agent_loop，用户需要重新输入来继续。


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms06 >> \033[0m")
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