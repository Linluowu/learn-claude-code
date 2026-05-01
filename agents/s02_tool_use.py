#!/usr/bin/env python3
# 指定解释器为 python3，shebang 行，用于在类 Unix 系统上直接运行脚本。

# Harness: tool dispatch -- expanding what the model can reach.
# Harness（套具）：工具调度 —— 扩展模型能够触及的能力范围。
# 与 s01 相比，本文件增加了多种工具（read_file、write_file、edit_file），
# 使模型不仅能执行 shell 命令，还能读取、写入和编辑文件。

"""
s02_tool_use.py - Tools

s02_tool_use.py - 工具使用

The agent loop from s01 didn't change. We just added tools to the array
and a dispatch map to route calls.

与 s01 中的 Agent 循环相比，循环本身没有改变。
我们只是向工具数组中添加了更多工具，并创建了一个调度映射表来路由调用。

    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."

核心洞察："循环本身完全没有改变，我只是添加了更多工具。"
这体现了 Agent 架构的可扩展性 —— 核心循环不变，通过增加工具来增强能力。
"""

import os
# 导入 os 模块：用于与操作系统交互，读取环境变量、操作路径等。

import subprocess
# 导入 subprocess 模块：用于执行外部 shell 命令，创建子进程。

from pathlib import Path
# 从 pathlib 导入 Path 类：用于面向对象的路径操作。
# Path 提供了比 os.path 更现代、更直观的路径处理方法（如 / 运算符拼接路径、resolve() 解析绝对路径等）。

from anthropic import Anthropic
# 从 anthropic 包导入 Anthropic 类：Anthropic 官方 SDK，用于与 Claude API 通信。

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv：用于从 .env 文件加载环境变量。

load_dotenv(override=True)
# 加载当前目录下 .env 文件中的环境变量，override=True 表示覆盖已存在的环境变量。

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查是否设置了自定义 Anthropic API 基础 URL（如代理地址）。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 如果使用了自定义 base URL，移除认证令牌（可能代理不需要令牌认证）。

WORKDIR = Path.cwd()
# 设置工作目录为当前工作目录（Current Working Directory）。
# Path.cwd() 返回当前进程的工作目录的 Path 对象。
# 使用 Path 对象而非字符串，可以利用 pathlib 提供的丰富路径操作方法。

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端实例，base_url 从环境变量读取（未设置则使用默认值）。

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取模型 ID，使用 os.environ（非 os.getenv）表示这是必需配置，缺失会报错。

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."
# 系统提示词：设定模型角色为编程助手，告知当前工作目录，指示使用工具解决问题。
# "Act, don't explain" 要求模型直接行动，避免冗长解释。


def safe_path(p: str) -> Path:
    # 定义 safe_path 函数：安全检查文件路径，防止路径遍历攻击。
    # 参数 p: str —— 用户提供的相对路径字符串（可能来自模型生成的工具调用参数）。
    # 返回值 -> Path —— 返回经过安全检查后的绝对路径 Path 对象。
    # 安全意义：防止模型通过 "../../etc/passwd" 等路径遍历手段访问工作目录外的敏感文件。
    path = (WORKDIR / p).resolve()
    # 将相对路径转换为绝对路径：
    # - WORKDIR / p：使用 Path 的 / 运算符拼接工作目录和用户路径。
    # - .resolve()：解析为绝对路径，同时消除 .. 和 . 等符号链接和相对路径片段。
    #   例如："/home/user/project/../secret.txt" 会被解析为 "/home/user/secret.txt"。
    if not path.is_relative_to(WORKDIR):
        # 检查解析后的路径是否仍相对于 WORKDIR（即在工作目录内）。
        # is_relative_to() 方法：判断一个路径是否是另一个路径的子路径。
        # 例如：/home/user/project/file.txt 相对于 /home/user/project 返回 True。
        # 但 /home/user/secret.txt 相对于 /home/user/project 返回 False。
        raise ValueError(f"Path escapes workspace: {p}")
        # 如果路径逃逸出了工作目录（如 ../../etc/passwd），抛出 ValueError 异常。
        # 这是安全沙箱的关键防线，确保模型只能访问工作目录内的文件。
    return path
    # 路径安全检查通过，返回安全的绝对路径。


def run_bash(command: str) -> str:
    # 定义 run_bash 函数：执行 shell 命令，与 s01 中的版本基本一致。
    # 参数 command: str —— 要执行的 shell 命令字符串。
    # 返回值 -> str —— 命令执行的输出结果字符串。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 危险命令黑名单，防止执行破坏性操作。
    if any(d in command for d in dangerous):
        # 检查命令中是否包含任何危险关键词。
        return "Error: Dangerous command blocked"
        # 发现危险命令，拒绝执行并返回错误信息。
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
        # 使用 subprocess.run() 执行命令：
        # - shell=True：通过系统 shell 执行，支持管道、重定向等 shell 语法。
        # - cwd=WORKDIR：设置工作目录为 WORKDIR（当前工作目录）。
                           capture_output=True, text=True, timeout=120)
        # - capture_output=True：捕获 stdout 和 stderr。
        # - text=True：以文本模式返回输出。
        # - timeout=120：120 秒超时限制。
        out = (r.stdout + r.stderr).strip()
        # 合并标准输出和标准错误，去除首尾空白。
        return out[:50000] if out else "(no output)"
        # 返回输出内容（限制 50000 字符），空输出则返回 "(no output)"。
    except subprocess.TimeoutExpired:
        # 捕获超时异常。
        return "Error: Timeout (120s)"
        # 返回超时错误信息。


def run_read(path: str, limit: int = None) -> str:
    # 定义 run_read 函数：读取文件内容。
    # 参数 path: str —— 要读取的文件相对路径。
    # 参数 limit: int = None —— 可选参数，限制读取的最大行数。默认为 None 表示不限制。
    # 返回值 -> str —— 文件内容字符串。
    try:
        # try-except 块：捕获文件读取过程中的异常（如文件不存在、权限不足等）。
        text = safe_path(path).read_text()
        # 先通过 safe_path() 进行路径安全检查，然后调用 read_text() 读取文件全部内容。
        # safe_path(path) 返回 Path 对象，Path.read_text() 以 UTF-8 编码读取文件文本内容。
        lines = text.splitlines()
        # 将文件内容按行分割为字符串列表。
        # splitlines() 方法根据各种换行符（\n、\r\n、\r）将文本分割成多行。
        if limit and limit < len(lines):
            # 如果指定了 limit 且文件总行数超过限制：
            # - limit 为真值（非 None、非 0）：表示用户设置了行数限制。
            # - limit < len(lines)：实际行数超过了限制。
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
            # 截取前 limit 行，并在末尾添加省略提示，告知用户后面还有多少行未显示。
            # f-string 格式化：动态计算并显示省略的行数。
        return "\n".join(lines)[:50000]
        # 将处理后的行列表用换行符连接成一个字符串，并限制总长度不超过 50000 字符。
        # 限制长度是为了防止超大文件消耗过多 token，影响 API 调用效率。
    except Exception as e:
        # 捕获所有异常（文件不存在、权限不足、编码错误等）。
        return f"Error: {e}"
        # 返回错误信息，f-string 将异常对象转换为可读字符串。


def run_write(path: str, content: str) -> str:
    # 定义 run_write 函数：将内容写入文件。
    # 参数 path: str —— 要写入的文件相对路径。
    # 参数 content: str —— 要写入的文件内容字符串。
    # 返回值 -> str —— 操作结果信息。
    try:
        fp = safe_path(path)
        # 通过 safe_path() 安全检查获取目标文件的 Path 对象。
        fp.parent.mkdir(parents=True, exist_ok=True)
        # 确保文件的父目录存在：
        # - fp.parent：获取文件所在目录的 Path 对象。
        # - mkdir(parents=True)：递归创建所有不存在的父目录。
        #   例如：写入 "a/b/c/file.txt" 时，如果 a、b、c 目录都不存在，会全部创建。
        # - exist_ok=True：如果目录已存在，不抛出异常。
        fp.write_text(content)
        # 将 content 字符串写入文件，使用 UTF-8 编码。
        # 如果文件已存在，会覆盖原有内容。
        return f"Wrote {len(content)} bytes to {path}"
        # 返回成功信息，包含写入的字节数（实际上是字符数，UTF-8 编码下一个字符可能占多个字节，
        # 但 len(content) 返回的是 Unicode 字符数）。
    except Exception as e:
        # 捕获所有异常（权限不足、磁盘空间不足等）。
        return f"Error: {e}"
        # 返回错误信息。


def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 定义 run_edit 函数：在文件中替换指定的文本片段。
    # 参数 path: str —— 要编辑的文件相对路径。
    # 参数 old_text: str —— 要被替换的原始文本。
    # 参数 new_text: str —— 用于替换的新文本。
    # 返回值 -> str —— 操作结果信息。
    try:
        fp = safe_path(path)
        # 通过 safe_path() 安全检查获取目标文件的 Path 对象。
        content = fp.read_text()
        # 读取文件的完整内容。
        if old_text not in content:
            # 检查原始文本是否存在于文件内容中。
            return f"Error: Text not found in {path}"
            # 如果找不到要替换的文本，返回错误信息，避免意外修改。
        fp.write_text(content.replace(old_text, new_text, 1))
        # 替换文件内容中的指定文本：
        # - content.replace(old_text, new_text, 1)：将 old_text 替换为 new_text，
        #   第三个参数 1 表示只替换第一次出现（避免全局替换导致意外修改多处）。
        # - 将替换后的内容写回文件。
        return f"Edited {path}"
        # 返回成功信息。
    except Exception as e:
        # 捕获所有异常。
        return f"Error: {e}"
        # 返回错误信息。


# -- The dispatch map: {tool_name: handler} --
# -- 调度映射表：{工具名称: 处理函数} --
# 这个字典实现了工具名称到实际处理函数的映射，是工具调度机制的核心。
# 当模型请求调用某个工具时，harness 通过这个映射表找到对应的处理函数并执行。
TOOL_HANDLERS = {
    # TOOL_HANDLERS 字典：键是工具名称（字符串），值是对应的处理函数。
    "bash":       lambda **kw: run_bash(kw["command"]),
    # "bash" 工具的处理函数：
    # - lambda **kw：使用关键字参数解包的匿名函数，接收所有传入的参数。
    # - kw["command"]：从参数字典中提取 command 参数，传递给 run_bash()。
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    # "read_file" 工具的处理函数：
    # - kw["path"]：提取 path 参数（必需）。
    # - kw.get("limit")：提取 limit 参数（可选），使用 get() 方法避免参数不存在时抛出 KeyError。
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    # "write_file" 工具的处理函数：
    # - kw["path"]：目标文件路径。
    # - kw["content"]：要写入的内容。
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    # "edit_file" 工具的处理函数：
    # - kw["path"]：目标文件路径。
    # - kw["old_text"]：要替换的原始文本。
    # - kw["new_text"]：替换后的新文本。
}

TOOLS = [
    # TOOLS 列表：定义模型可见的所有工具及其 JSON Schema 参数规范。
    # 这个列表会传递给 LLM API，模型根据这些信息决定何时调用哪个工具。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    # bash 工具定义：
    # - "name": "bash"：工具名称，模型调用时使用这个名称。
    # - "description": 工具描述，帮助模型理解工具用途。
    # - "input_schema": JSON Schema 定义输入参数结构。
    #   - "type": "object"：参数是一个对象（字典）。
    #   - "properties": 对象中允许的字段。
    #     - "command": {"type": "string"}：command 字段，字符串类型。
    #   - "required": ["command"]：command 是必填字段。
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    # read_file 工具定义：
    # - "path": 文件路径，字符串类型，必填。
    # - "limit": 行数限制，整数类型，可选（不在 required 中）。
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    # write_file 工具定义：
    # - "path": 文件路径，必填。
    # - "content": 文件内容，必填。
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    # edit_file 工具定义：
    # - "path": 文件路径，必填。
    # - "old_text": 要替换的原始文本，必填。
    # - "new_text": 新文本，必填。
]


def agent_loop(messages: list):
    # 定义 agent_loop 函数：Agent 核心循环，与 s01 基本相同。
    # 参数 messages: list —— 消息历史列表。
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        # 调用 Claude API，传入模型、系统提示词、消息历史、可用工具列表和最大 token 限制。
        messages.append({"role": "assistant", "content": response.content})
        # 将模型的回复追加到消息历史。
        if response.stop_reason != "tool_use":
            # 如果模型没有调用工具，说明任务已完成。
            return
            # 结束循环。
        results = []
        # 初始化结果列表，存储所有工具调用的执行结果。
        for block in response.content:
            # 遍历模型回复中的每个内容块。
            if block.type == "tool_use":
                # 检查是否为工具调用请求。
                handler = TOOL_HANDLERS.get(block.name)
                # 从调度映射表中查找对应工具的处理函数。
                # .get(block.name) 使用 get 方法，如果工具名称不存在则返回 None（而非抛出异常）。
                output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                # 如果找到了处理函数（handler 为真值），调用它并传入工具参数。
                # 使用 **block.input 将工具参数字典解包为关键字参数。
                # 如果没有找到处理函数，返回 "Unknown tool" 错误信息。
                print(f"> {block.name}:")
                # 打印正在执行的工具名称，带 ">" 前缀标识。
                print(output[:200])
                # 打印工具输出的前 200 个字符。
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
                # 构建工具结果，包含类型、工具调用 ID 和输出内容。
        messages.append({"role": "user", "content": results})
        # 将工具执行结果作为用户消息追加到对话历史，反馈给模型。


if __name__ == "__main__":
    # 当脚本直接运行时执行。
    history = []
    # 初始化空的消息历史列表。
    while True:
        # 无限循环：持续接收用户输入。
        try:
            query = input("\033[36ms02 >> \033[0m")
            # 显示青色提示符 "s02 >> "，接收用户输入。
        except (EOFError, KeyboardInterrupt):
            # 捕获 EOF（Ctrl+D）或中断（Ctrl+C）。
            break
            # 退出循环。
        if query.strip().lower() in ("q", "exit", ""):
            # 检查是否为退出命令。
            break
            # 退出循环。
        history.append({"role": "user", "content": query})
        # 将用户输入追加到消息历史。
        agent_loop(history)
        # 启动 Agent 循环处理用户输入。
        response_content = history[-1]["content"]
        # 获取最后一条消息的内容。
        if isinstance(response_content, list):
            # 如果内容是列表（模型回复包含多个块）。
            for block in response_content:
                # 遍历每个内容块。
                if hasattr(block, "text"):
                    # 检查是否有 text 属性（文本块）。
                    print(block.text)
                    # 打印文本内容。
        print()
        # 打印空行分隔。
