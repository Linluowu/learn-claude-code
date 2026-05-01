#!/usr/bin/env python3
# 指定解释器为 python3，这是一个 shebang 行，用于在类 Unix 系统上直接运行脚本。

# Harness: the loop -- the model's first connection to the real world.
# Harness（套具/框架）：这是核心的 Agent 循环 —— 模型首次与现实世界建立连接的桥梁。
# "Harness" 指的是支撑模型运行的基础设施，模型本身只是语言模型，
# 这套代码赋予它执行 shell 命令、读取文件等能力。

"""
s01_agent_loop.py - The Agent Loop

s01_agent_loop.py - 核心 Agent 循环

The entire secret of an AI coding agent in one pattern:
一个 AI 编程智能体的全部秘密，都藏在这一个模式里：

    while stop_reason == "tool_use":
    # 当模型停止的原因是"使用了工具"时，持续循环：
    # - stop_reason 是 LLM API 返回的字段，说明模型为什么停止生成文本
    # - "tool_use" 表示模型决定调用一个外部工具（如执行 shell 命令）
    # - 这意味着模型还没有给出最终答案，它需要先执行工具，观察结果，再继续思考
        response = LLM(messages, tools)
        # 调用大语言模型（LLM），传入当前的消息历史和可用工具列表
        # 模型会根据对话历史决定是回答用户，还是调用某个工具
        execute tools
        # 如果模型决定调用工具，harness 负责实际执行这些工具
        append results
        # 将工具执行的结果追加到消息历史中，反馈给模型
        # 模型在下一轮会看到工具输出，据此决定下一步行动

    +----------+      +-------+      +---------+
    |   User   | ---> |  LLM  | ---> |  Tool   |
    |  prompt  |      |       |      | execute |
    +----------+      +---+---+      +----+----+
                          ^               |
                          |   tool_result |
                          +---------------+
                          (loop continues)

This is the core loop: feed tool results back to the model
until the model decides to stop. Production agents layer
policy, hooks, and lifecycle controls on top.

这就是核心循环：持续将工具执行结果反馈给模型，
直到模型决定停止调用工具（给出最终答案）。
生产级智能体还会在此基础上叠加策略控制、生命周期钩子、权限管理等。
"""

import os
# 导入 os 模块：用于与操作系统交互，例如获取当前工作目录（os.getcwd()）、读取环境变量等。

import subprocess
# 导入 subprocess 模块：用于在 Python 中创建新进程（子进程），执行外部命令（如 shell 命令），
# 并捕获其标准输出和标准错误。这是让模型能够"动手"执行系统命令的关键模块。

try:
    # try-except 块：尝试导入 readline 模块，用于增强交互式命令行体验。
    # readline 提供行编辑功能（如方向键移动光标、历史记录回溯等）。
    import readline
    # #143 UTF-8 backspace fix for macOS libedit
    # 问题 #143 的修复：macOS 使用的是 libedit 而不是 GNU readline，
    # 在处理 UTF-8 编码的字符时，退格键（backspace）可能会有问题。
    # 下面几行配置用于修复这些兼容性问题，确保中文、日文等多字节字符能正确编辑。
    readline.parse_and_bind('set bind-tty-special-chars off')
    # 关闭终端特殊字符绑定，防止某些快捷键被终端截获。
    readline.parse_and_bind('set input-meta on')
    # 开启 8 位输入模式，允许接收扩展字符集（如 UTF-8 多字节字符）。
    readline.parse_and_bind('set output-meta on')
    # 开启 8 位输出模式，允许输出扩展字符集。
    readline.parse_and_bind('set convert-meta off')
    # 关闭 meta 键转换，防止将 8 位字符错误地转换为 escape 序列。
    readline.parse_and_bind('set enable-meta-keybindings on')
    # 启用 meta 键组合绑定，支持 Alt 键组合快捷键。
except ImportError:
    # 如果 readline 模块不可用（例如在某些 Windows 环境中），
    # 静默忽略导入错误，程序继续正常运行，只是缺少行编辑功能。
    pass

from anthropic import Anthropic
# 从 anthropic 包导入 Anthropic 类：Anthropic 的官方 Python SDK，
# 用于与 Claude 大语言模型 API 通信。通过这个客户端，我们可以发送消息给 Claude 并接收回复。

from dotenv import load_dotenv
# 从 python-dotenv 包导入 load_dotenv 函数：用于从 .env 文件中加载环境变量。
# .env 文件通常存放敏感配置（如 API 密钥），不会被提交到版本控制系统中。

load_dotenv(override=True)
# 加载当前目录下 .env 文件中的环境变量。
# override=True 表示如果环境变量已存在，用 .env 文件中的值覆盖它。
# 这样可以从配置文件而非硬编码代码中读取 API 密钥等敏感信息。

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查环境变量 ANTHROPIC_BASE_URL 是否已设置。
    # 这个变量用于指定自定义的 Anthropic API 基础 URL（如代理服务器地址）。
    # 如果设置了自定义 base URL，说明可能在使用代理或本地服务。
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 从环境变量中移除 ANTHROPIC_AUTH_TOKEN。
    # 原因：当使用自定义 base URL（如内部代理）时，可能不需要或不能使用 auth token。
    # pop 的第二个参数 None 是默认值，表示如果该变量不存在也不报错。

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端实例。
# base_url 参数：从环境变量读取自定义 API 地址，如果未设置则使用默认值（官方 API 地址）。
# 这个 client 对象将用于后续所有与 Claude 模型的通信。

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取模型 ID（如 "claude-opus-4-7" 等）。
# 使用 os.environ 而非 os.getenv，表示 MODEL_ID 是必须设置的环境变量，
# 如果不存在会直接抛出 KeyError，强制用户必须配置模型 ID。

SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."
# 定义系统提示词（System Prompt），这是发送给模型的"元指令"，设定模型的角色和行为准则。
# - f-string 格式化字符串，动态插入当前工作目录路径（os.getcwd()）。
# - "You are a coding agent"：告诉模型它是一个编程助手。
# - "at {os.getcwd()}"：告诉模型当前所在目录，让它知道文件系统的上下文。
# - "Use bash to solve tasks"：指示模型使用 bash 命令来解决任务。
# - "Act, don't explain"：要求模型直接行动（执行命令），不要过多解释。

TOOLS = [{
    # TOOLS 列表：定义模型可以使用的工具集合。每个工具都是一个字典，描述工具的名称、功能和参数格式。
    # 这是"函数调用"（Function Calling）机制的核心 —— 模型看到这个列表后，
    # 可以决定何时调用哪个工具，并生成符合 input_schema 的参数。
    "name": "bash",
    # 工具名称：bash。模型将使用这个名称来请求执行 shell 命令。
    "description": "Run a shell command.",
    # 工具描述：告诉模型这个工具的用途是"运行一个 shell 命令"。
    # 模型根据描述来决定是否需要调用这个工具。
    "input_schema": {
        # input_schema 定义工具所需的输入参数结构（JSON Schema 格式）。
        # 模型会根据这个 schema 生成正确的参数。
        "type": "object",
        # 参数类型为对象（即字典/键值对结构）。
        "properties": {"command": {"type": "string"}},
        # properties 定义对象中的各个字段：
        # - command：字段名，类型为字符串（string），表示要执行的 shell 命令。
        "required": ["command"],
        # required 列表：声明哪些字段是必填的。这里 command 是必填参数。
    },
}]


def run_bash(command: str) -> str:
    # 定义 run_bash 函数：实际执行 bash 命令的核心工具函数。
    # 参数 command: str —— 要执行的 shell 命令字符串，类型为字符串。
    # 返回值 -> str —— 函数返回命令执行的输出结果，类型为字符串。
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 定义危险命令黑名单列表：
    # - "rm -rf /"：强制递归删除根目录，会摧毁整个系统。
    # - "sudo"：以超级用户权限执行命令，风险过高。
    # - "shutdown" / "reboot"：关机和重启命令，会中断服务。
    # - "> /dev/"：重定向到设备文件的操作，可能破坏系统设备。
    # 这是安全防护的第一道防线，防止 AI 意外或恶意执行破坏性操作。
    if any(d in command for d in dangerous):
        # 检查命令字符串中是否包含任何危险关键词。
        # any() 函数：只要有一个条件为 True，就返回 True。
        # 生成器表达式 (d in command for d in dangerous)：遍历每个危险词，检查是否在命令中。
        return "Error: Dangerous command blocked"
        # 如果检测到危险命令，立即返回错误信息，拒绝执行，不调用 subprocess。
    try:
        # try-except 块：捕获执行命令过程中可能出现的异常。
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
        # subprocess.run()：执行 shell 命令的核心函数。
        # - command：要执行的命令字符串。
        # - shell=True：通过系统 shell 执行命令，支持管道、重定向等 shell 特性。
        # - cwd=os.getcwd()：设置命令的工作目录为当前目录（Current Working Directory）。
        #   返回结果 r 是一个 CompletedProcess 对象，包含命令的执行结果。
                           capture_output=True, text=True, timeout=120)
        # - capture_output=True：捕获命令的标准输出（stdout）和标准错误（stderr），
        #   而不是直接打印到终端。
        # - text=True：以文本模式（字符串）返回输出，而不是字节模式（bytes）。
        # - timeout=120：命令最长执行 120 秒，超时后会抛出 TimeoutExpired 异常。
        out = (r.stdout + r.stderr).strip()
        # 将标准输出和标准错误合并，并去除首尾空白字符。
        # r.stdout：命令的正常输出。
        # r.stderr：命令的错误输出。
        # .strip()：去除字符串首尾的空白、换行符等。
        return out[:50000] if out else "(no output)"
        # 返回命令输出，但限制最大长度为 50000 个字符。
        # 如果输出为空字符串，返回 "(no output)" 提示没有输出。
        # 限制长度是为了防止超大输出（如 cat 大文件）消耗过多 token。
    except subprocess.TimeoutExpired:
        # 捕获超时异常：命令执行时间超过 120 秒。
        return "Error: Timeout (120s)"
        # 返回超时错误信息，告知用户命令已超时。
    except (FileNotFoundError, OSError) as e:
        # 捕获文件未找到或操作系统错误：
        # - FileNotFoundError：命令不存在（如输入了不存在的命令）。
        # - OSError：其他操作系统级错误（如权限不足）。
        return f"Error: {e}"
        # 返回具体的错误信息，f-string 格式化将异常对象 e 转换为字符串。


# -- The core pattern: a while loop that calls tools until the model stops --
# -- 核心模式：一个 while 循环，持续调用工具，直到模型决定停止 --
def agent_loop(messages: list):
    # 定义 agent_loop 函数：Agent 的核心循环函数。
    # 参数 messages: list —— 消息历史列表，包含用户和助手的对话记录。
    # 每个消息是一个字典，格式为 {"role": "user"|"assistant", "content": "..."}。
    while True:
        # 无限循环：Agent 会持续运行，直到模型决定不再调用工具。
        response = client.messages.create(
        # 调用 Claude API 创建新消息。
        # client.messages.create 是 Anthropic SDK 的方法，向模型发送请求。
            model=MODEL, system=SYSTEM, messages=messages,
            # - model=MODEL：指定使用的模型 ID（如 claude-opus-4-7）。
            # - system=SYSTEM：传入系统提示词，设定模型的角色和行为。
            # - messages=messages：传入完整的对话历史，模型基于上下文做出决策。
            tools=TOOLS, max_tokens=8000,
            # - tools=TOOLS：传入可用工具列表，模型可以看到并决定调用哪些工具。
            # - max_tokens=8000：限制模型单次回复的最大 token 数，防止生成过长文本。
        )
        # Append assistant turn
        # 追加助手回合：将模型的回复添加到消息历史中。
        messages.append({"role": "assistant", "content": response.content})
        # - "role": "assistant"：标记这条消息来自 AI 助手（Claude）。
        # - response.content：模型的回复内容，可能包含文本块和工具调用块。
        # If the model didn't call a tool, we're done
        # 如果模型没有调用工具，说明任务已完成，循环结束。
        if response.stop_reason != "tool_use":
            # response.stop_reason 是 API 返回的停止原因：
            # - "tool_use"：模型决定调用工具，需要继续循环。
            # - "end_turn" 或 "stop_sequence"：模型给出了最终回答，任务完成。
            return
            # 返回 None，结束 agent_loop 函数，将控制权交还给主程序。
        # Execute each tool call, collect results
        # 执行模型发起的每个工具调用，收集结果。
        results = []
        # 初始化结果列表，用于存储所有工具调用的执行结果。
        for block in response.content:
            # 遍历模型回复中的每个内容块。
            # response.content 是一个列表，可能包含文本块（text）和工具调用块（tool_use）。
            if block.type == "tool_use":
                # 检查内容块类型是否为 "tool_use"（工具调用请求）。
                # 模型通过这种方式告诉 harness："我需要执行某个工具"。
                print(f"\033[33m$ {block.input['command']}\033[0m")
                # 打印正在执行的命令到终端，带有 ANSI 颜色码：
                # - \033[33m：黄色前景色，使命令高亮显示。
                # - \033[0m：重置颜色，恢复正常显示。
                # - block.input['command']：模型生成的命令参数。
                output = run_bash(block.input["command"])
                # 调用 run_bash 函数执行模型请求的 shell 命令，获取执行结果。
                # block.input["command"]：从工具调用块中提取 command 参数。
                print(output[:200])
                # 打印命令输出的前 200 个字符到终端，让用户看到执行结果。
                # 截断输出是为了避免终端被大量输出刷屏。
                results.append({"type": "tool_result", "tool_use_id": block.id,
                # 构建工具结果字典，格式符合 Anthropic API 的要求：
                # - "type": "tool_result"：标记这是一个工具执行结果。
                # - "tool_use_id": block.id：关联对应的工具调用请求 ID，
                #   让模型知道这个结果对应哪个工具调用。
                                "content": output})
                # - "content": output：工具执行的实际输出内容。
        messages.append({"role": "user", "content": results})
        # 将工具执行结果作为"用户"消息追加到对话历史中。
        # 注意：这里 role 是 "user"，但实际上内容是工具结果。
        # 这是 Anthropic API 的约定 —— 工具结果以用户消息的形式反馈给模型。


if __name__ == "__main__":
    # 当脚本直接运行时（而不是被导入为模块时）执行以下代码。
    # __name__ == "__main__" 是 Python 的惯用写法，区分"直接运行"和"被导入"。
    history = []
    # 初始化对话历史列表，用于存储用户和模型的消息。
    # 这是一个空列表，随着对话进行会不断追加消息。
    while True:
        # 无限循环：持续接收用户输入，直到用户选择退出。
        try:
            query = input("\033[36ms01 >> \033[0m")
            # 接收用户输入，显示彩色提示符 "s01 >> "。
            # - \033[36m：青色前景色。
            # - \033[0m：重置颜色。
            # - "s01"：标识当前是 s01 会话。
            # input() 函数会阻塞等待用户输入一行文本。
        except (EOFError, KeyboardInterrupt):
            # 捕获两种异常：
            # - EOFError：用户按下 Ctrl+D（Unix）或 Ctrl+Z+Enter（Windows），表示输入结束。
            # - KeyboardInterrupt：用户按下 Ctrl+C，表示中断程序。
            break
            # 跳出 while 循环，结束程序。
        if query.strip().lower() in ("q", "exit", ""):
            # 检查用户输入是否为退出命令：
            # - .strip()：去除首尾空白字符。
            # - .lower()：转为小写，实现大小写不敏感的匹配。
            # - 支持 "q"、"exit" 或空字符串 "" 三种退出方式。
            break
            # 跳出循环，结束程序。
        history.append({"role": "user", "content": query})
        # 将用户输入作为用户消息追加到对话历史中。
        # - "role": "user"：标记消息来源为用户。
        # - "content": query：消息内容为用户的输入文本。
        agent_loop(history)
        # 调用 agent_loop 函数，启动 Agent 循环。
        # 模型会处理用户输入，决定是直接回答还是调用工具。
        # 循环结束后，history 列表会被更新，追加模型的回复。
        response_content = history[-1]["content"]
        # 获取 history 列表中最后一条消息的内容。
        # history[-1] 是最后一条消息，["content"] 提取其内容字段。
        if isinstance(response_content, list):
            # 检查最后一条消息的内容是否为列表类型。
            # 当模型调用工具后，API 返回的内容是列表（包含多个内容块）。
            for block in response_content:
                # 遍历内容列表中的每个块。
                if hasattr(block, "text"):
                    # 检查内容块是否有 text 属性（即是否为文本块）。
                    # 工具调用块（tool_use）没有 text 属性，只有 input 等属性。
                    print(block.text)
                    # 打印文本块的内容到终端，显示模型的回复文本。
        print()
        # 打印一个空行，美化输出格式，让每轮对话之间有间隔。
