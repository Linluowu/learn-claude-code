#!/usr/bin/env python3
# 指定脚本解释器为 python3

# Harness: team mailboxes -- multiple models, coordinated through files.
# Harness：团队邮箱 —— 多个模型通过文件协调工作。
# 本文件实现多智能体团队系统，每个队友有自己的线程和基于文件的 JSONL 收件箱。
# 通过追加写入的收件箱实现队友间的异步通信。

"""
s09_agent_teams.py - Agent Teams

s09_agent_teams.py - 智能体团队

Persistent named agents with file-based JSONL inboxes. Each teammate runs
its own agent loop in a separate thread. Communication via append-only inboxes.

持久化的命名智能体，使用基于文件的 JSONL 收件箱。每个队友在独立的线程中
运行自己的 Agent 循环。通过追加写入的收件箱进行通信。

    Subagent (s04):  spawn -> execute -> return summary -> destroyed
    Teammate (s09):  spawn -> work -> idle -> work -> ... -> shutdown

    .team/config.json                   .team/inbox/
    +----------------------------+      +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |
    |  "members": [              |      | bob.jsonl        |
    |    {"name":"alice",        |      | lead.jsonl       |
    |     "role":"coder",        |      +------------------+
    |     "status":"idle"}       |
    |  ]}                        |      send_message("alice", "fix bug"):
    +----------------------------+        open("alice.jsonl", "a").write(msg)

                                        read_inbox("alice"):
    spawn_teammate("alice","coder",...)   msgs = [json.loads(l) for l in ...]
         |                                open("alice.jsonl", "w").close()
         v                                return msgs  # drain
    Thread: alice             Thread: bob
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |
    | status: working  |      | status: idle     |
    | ... runs tools   |      | ... waits ...    |
    | status -> idle   |      |                  |
    +------------------+      +------------------+

    5 message types (all declared, not all handled here):
    +-------------------------+-----------------------------------+
    | message                 | Normal text message               |
    | broadcast               | Sent to all teammates             |
    | shutdown_request        | Request graceful shutdown (s10)   |
    | shutdown_response       | Approve/reject shutdown (s10)     |
    | plan_approval_response  | Approve/reject plan (s10)         |
    +-------------------------+-----------------------------------+

Key insight: "Teammates that can talk to each other."

核心洞察："能够相互交谈的队友。"
"""

import json
# 导入 json 模块

import os
# 导入 os 模块

import subprocess
# 导入 subprocess 模块

import threading
# 导入 threading 模块，用于创建多线程

import time
# 导入 time 模块

from pathlib import Path
# 从 pathlib 导入 Path

from anthropic import Anthropic
# 从 anthropic 导入 Anthropic

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv

load_dotenv(override=True)
# 加载环境变量

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查自定义 base URL
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 移除认证令牌

WORKDIR = Path.cwd()
# 设置工作目录

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic 客户端

MODEL = os.environ["MODEL_ID"]
# 读取模型 ID

TEAM_DIR = WORKDIR / ".team"
# 团队配置目录：工作目录下的 .team 隐藏文件夹

INBOX_DIR = TEAM_DIR / "inbox"
# 收件箱目录：.team/inbox/，每个队友一个 JSONL 文件

SYSTEM = f"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."
# 主智能体（队长）的系统提示词

VALID_MSG_TYPES = {
    # 有效的消息类型集合
    "message",
    # 普通文本消息
    "broadcast",
    # 广播消息（发送给所有队友）
    "shutdown_request",
    # 关闭请求（请求优雅关闭，在 s10 中处理）
    "shutdown_response",
    # 关闭响应（批准/拒绝关闭请求，在 s10 中处理）
    "plan_approval_response",
    # 计划审批响应（批准/拒绝计划，在 s10 中处理）
}


# -- MessageBus: JSONL inbox per teammate --
# -- MessageBus：每个队友的 JSONL 收件箱 --
class MessageBus:
    # MessageBus 类：管理队友间的消息发送和接收
    def __init__(self, inbox_dir: Path):
        # 构造函数
        # 参数 inbox_dir: Path —— 收件箱目录
        self.dir = inbox_dir
        # 保存收件箱目录
        self.dir.mkdir(parents=True, exist_ok=True)
        # 创建收件箱目录（如果不存在），parents=True 递归创建父目录

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        # send 方法：发送消息到指定队友的收件箱
        # 参数 sender: str —— 发送者名称
        # 参数 to: str —— 接收者名称
        # 参数 content: str —— 消息内容
        # 参数 msg_type: str = "message" —— 消息类型，默认为普通消息
        # 参数 extra: dict = None —— 额外的字段，可选
        # 返回值 -> str —— 发送结果
        if msg_type not in VALID_MSG_TYPES:
            # 检查消息类型是否有效
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
            # 无效则返回错误
        msg = {
            "type": msg_type,
            # 消息类型
            "from": sender,
            # 发送者
            "content": content,
            # 消息内容
            "timestamp": time.time(),
            # 时间戳（Unix 时间戳，秒）
        }
        if extra:
            # 如果有额外字段
            msg.update(extra)
            # 合并到消息字典中
        inbox_path = self.dir / f"{to}.jsonl"
        # 构建收件箱文件路径，以接收者名称命名
        with open(inbox_path, "a") as f:
            # 以追加模式打开文件
            f.write(json.dumps(msg) + "\n")
            # 将消息序列化为 JSON 并写入文件，每条消息一行（JSONL 格式）
        return f"Sent {msg_type} to {to}"
        # 返回发送成功信息

    def read_inbox(self, name: str) -> list:
        # read_inbox 方法：读取并清空指定队友的收件箱
        # 参数 name: str —— 队友名称
        # 返回值 -> list —— 消息列表
        inbox_path = self.dir / f"{name}.jsonl"
        # 构建收件箱文件路径
        if not inbox_path.exists():
            # 如果文件不存在
            return []
            # 返回空列表
        messages = []
        # 消息列表
        for line in inbox_path.read_text().strip().splitlines():
            # 读取文件内容，去除首尾空白，按行分割
            if line:
                # 跳过空行
                messages.append(json.loads(line))
                # 解析 JSON 并添加到列表
        inbox_path.write_text("")
        # 清空收件箱文件（写入空字符串）
        # 这就是 "drain"（清空）操作：读取后删除所有消息
        return messages
        # 返回消息列表

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        # broadcast 方法：向所有队友广播消息
        # 参数 sender: str —— 发送者名称
        # 参数 content: str —— 消息内容
        # 参数 teammates: list —— 队友名称列表
        # 返回值 -> str —— 广播结果
        count = 0
        # 计数器
        for name in teammates:
            # 遍历所有队友
            if name != sender:
                # 不发送给自己
                self.send(sender, name, content, "broadcast")
                # 发送广播消息
                count += 1
                # 计数
        return f"Broadcast to {count} teammates"
        # 返回广播结果


BUS = MessageBus(INBOX_DIR)
# 创建全局 MessageBus 实例


# -- TeammateManager: persistent named agents with config.json --
# -- TeammateManager：使用 config.json 的持久化命名智能体 --
class TeammateManager:
    # TeammateManager 类：管理队友的创建、状态跟踪和生命周期
    def __init__(self, team_dir: Path):
        # 构造函数
        # 参数 team_dir: Path —— 团队目录
        self.dir = team_dir
        # 保存团队目录
        self.dir.mkdir(exist_ok=True)
        # 创建目录（如果不存在）
        self.config_path = self.dir / "config.json"
        # 配置文件路径：.team/config.json
        self.config = self._load_config()
        # 加载团队配置
        self.threads = {}
        # threads：字典，存储每个队友的线程对象

    def _load_config(self) -> dict:
        # _load_config 方法：加载团队配置
        # 返回值 -> dict —— 配置字典
        if self.config_path.exists():
            # 如果配置文件存在
            return json.loads(self.config_path.read_text())
            # 读取并解析 JSON
        return {"team_name": "default", "members": []}
        # 默认配置：团队名为 "default"，成员为空列表

    def _save_config(self):
        # _save_config 方法：保存团队配置到文件
        self.config_path.write_text(json.dumps(self.config, indent=2))
        # 将配置序列化为 JSON 并写入文件，2 空格缩进

    def _find_member(self, name: str) -> dict:
        # _find_member 方法：查找指定名称的队友
        # 参数 name: str —— 队友名称
        # 返回值 -> dict —— 队友字典，如果不存在返回 None
        for m in self.config["members"]:
            # 遍历所有成员
            if m["name"] == name:
                # 找到匹配的队友
                return m
                # 返回队友信息
        return None
        # 未找到则返回 None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        # spawn 方法：创建新的队友
        # 参数 name: str —— 队友名称
        # 参数 role: str —— 角色（如 "coder"、"reviewer"）
        # 参数 prompt: str —— 初始提示词/任务描述
        # 返回值 -> str —— 创建结果
        member = self._find_member(name)
        # 查找是否已存在该队友
        if member:
            # 如果存在
            if member["status"] not in ("idle", "shutdown"):
                # 如果状态不是 idle 或 shutdown（即还在工作中）
                return f"Error: '{name}' is currently {member['status']}"
                # 返回错误：队友正在工作中
            member["status"] = "working"
            # 更新状态为 working
            member["role"] = role
            # 更新角色
        else:
            # 如果不存在
            member = {"name": name, "role": role, "status": "working"}
            # 创建新队友字典
            self.config["members"].append(member)
            # 添加到成员列表
        self._save_config()
        # 保存配置
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        # 创建新线程：
        # - target=self._teammate_loop：线程执行的目标函数
        # - args=(name, role, prompt)：传递给目标函数的参数
        # - daemon=True：守护线程，主线程结束时自动终止
        self.threads[name] = thread
        # 保存线程引用
        thread.start()
        # 启动线程
        return f"Spawned '{name}' (role: {role})"
        # 返回创建成功信息

    def _teammate_loop(self, name: str, role: str, prompt: str):
        # _teammate_loop 方法：队友的主循环（在独立线程中运行）
        # 参数 name: str —— 队友名称
        # 参数 role: str —— 角色
        # 参数 prompt: str —— 初始提示词
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Use send_message to communicate. Complete your task."
        )
        # 队友的系统提示词：告知自己的名称、角色和工作目录
        messages = [{"role": "user", "content": prompt}]
        # 初始化消息历史，只包含初始提示词
        tools = self._teammate_tools()
        # 获取队友可用的工具列表
        for _ in range(50):
            # 最多运行 50 轮对话（安全限制）
            inbox = BUS.read_inbox(name)
            # 读取并清空自己的收件箱
            for msg in inbox:
                # 将收件箱中的每条消息添加到消息历史
                messages.append({"role": "user", "content": json.dumps(msg)})
                # 将消息序列化为 JSON 字符串作为用户消息
            try:
                response = client.messages.create(
                    model=MODEL,
                    system=sys_prompt,
                    messages=messages,
                    tools=tools,
                    max_tokens=8000,
                )
                # 调用 Claude API
            except Exception:
                # 如果 API 调用失败
                break
                # 跳出循环
            messages.append({"role": "assistant", "content": response.content})
            # 追加模型回复
            if response.stop_reason != "tool_use":
                # 如果没有调用工具
                break
                # 跳出循环
            results = []
            # 结果列表
            for block in response.content:
                # 遍历内容块
                if block.type == "tool_use":
                    # 如果是工具调用
                    output = self._exec(name, block.name, block.input)
                    # 执行工具调用
                    print(f"  [{name}] {block.name}: {str(output)[:120]}")
                    # 打印执行结果（带队友名称前缀）
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    })
                    # 构建工具结果
            messages.append({"role": "user", "content": results})
            # 追加结果到消息历史
        member = self._find_member(name)
        # 查找队友信息
        if member and member["status"] != "shutdown":
            # 如果队友存在且状态不是 shutdown
            member["status"] = "idle"
            # 更新状态为 idle（空闲）
            self._save_config()
            # 保存配置

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # _exec 方法：执行工具调用
        # 参数 sender: str —— 发送者（队友名称）
        # 参数 tool_name: str —— 工具名称
        # 参数 args: dict —— 工具参数
        # 返回值 -> str —— 执行结果
        # these base tools are unchanged from s02
        # 这些基础工具与 s02 中的相同
        if tool_name == "bash":
            return _run_bash(args["command"])
        if tool_name == "read_file":
            return _run_read(args["path"])
        if tool_name == "write_file":
            return _run_write(args["path"], args["content"])
        if tool_name == "edit_file":
            return _run_edit(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
            # send_message 工具：发送消息给另一个队友
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)
            # read_inbox 工具：读取自己的收件箱
        return f"Unknown tool: {tool_name}"
        # 未知工具返回错误

    def _teammate_tools(self) -> list:
        # _teammate_tools 方法：返回队友可用的工具列表
        # 返回值 -> list —— 工具定义列表
        # these base tools are unchanged from s02
        return [
            {"name": "bash", "description": "Run a shell command.",
             "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
            {"name": "read_file", "description": "Read file contents.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
            {"name": "write_file", "description": "Write content to file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
            {"name": "edit_file", "description": "Replace exact text in file.",
             "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
            {"name": "send_message", "description": "Send message to a teammate.",
             "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
            # send_message 工具定义：发送消息给队友
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            # read_inbox 工具定义：读取并清空收件箱
        ]

    def list_all(self) -> str:
        # list_all 方法：列出所有队友
        # 返回值 -> str —— 格式化的队友列表
        if not self.config["members"]:
            # 如果没有成员
            return "No teammates."
            # 返回提示
        lines = [f"Team: {self.config['team_name']}"]
        # 第一行显示团队名称
        for m in self.config["members"]:
            # 遍历所有成员
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
            # 格式化每个队友的信息
        return "\n".join(lines)
        # 返回格式化的列表

    def member_names(self) -> list:
        # member_names 方法：返回所有队友的名称列表
        # 返回值 -> list —— 名称列表
        return [m["name"] for m in self.config["members"]]
        # 列表推导式提取所有队友名称


TEAM = TeammateManager(TEAM_DIR)
# 创建全局 TeammateManager 实例


# -- Base tool implementations (these base tools are unchanged from s02) --
# -- 基础工具实现（与 s02 中的相同）--
def _safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def _run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def _run_read(path: str, limit: int = None) -> str:
    try:
        lines = _safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def _run_write(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def _run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- Lead tool dispatch (9 tools) --
# -- 队长工具调度（9 个工具）--
TOOL_HANDLERS = {
    "bash":            lambda **kw: _run_bash(kw["command"]),
    "read_file":       lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":      lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":       lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":  lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    # spawn_teammate 工具：创建新队友
    "list_teammates":  lambda **kw: TEAM.list_all(),
    # list_teammates 工具：列出所有队友
    "send_message":    lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    # send_message 工具：队长发送消息给队友
    "read_inbox":      lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    # read_inbox 工具：队长读取自己的收件箱
    "broadcast":       lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    # broadcast 工具：队长向所有队友广播消息
}

# these base tools are unchanged from s02
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "spawn_teammate", "description": "Spawn a persistent teammate that runs in its own thread.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    # spawn_teammate 工具定义：创建持久化队友，在独立线程中运行
    {"name": "list_teammates", "description": "List all teammates with name, role, status.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate's inbox.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
]


def agent_loop(messages: list):
    # Agent 核心循环（队长）
    while True:
        inbox = BUS.read_inbox("lead")
        # 读取队长的收件箱
        if inbox:
            # 如果有新消息
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
            # 将收件箱内容作为用户消息追加到对话历史
            # 使用 <inbox> XML 标签包裹
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
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
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(output),
                })
        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            # 处理 /team 命令：显示所有队友
            print(TEAM.list_all())
            continue
            # 跳过本次循环，不进入 Agent 循环
        if query.strip() == "/inbox":
            # 处理 /inbox 命令：显示队长收件箱
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
            # 跳过本次循环
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
