#!/usr/bin/env python3
# 指定脚本解释器为 python3

# Harness: autonomy -- models that find work without being told.
# Harness：自主性 —— 无需被告知就能找到工作的模型。
# 本文件在 s10 的基础上增加了空闲循环和任务板轮询功能，
# 使队友能够在完成任务后自动领取新任务。

"""
s11_autonomous_agents.py - Autonomous Agents

s11_autonomous_agents.py - 自主智能体

Idle cycle with task board polling, auto-claiming unclaimed tasks, and
identity re-injection after context compression. Builds on s10's protocols.

空闲循环 + 任务板轮询 + 自动领取未认领任务 + 上下文压缩后的身份重新注入。
建立在 s10 的协议之上。

    Teammate lifecycle:
    +-------+
    | spawn |
    +---+---+
        |
        v
    +-------+  tool_use    +-------+
    | WORK  | <----------- |  LLM  |
    +---+---+              +-------+
        |
        | stop_reason != tool_use
        v
    +--------+
    | IDLE   | poll every 5s for up to 60s
    +---+----+
        |
        +---> check inbox -> message? -> resume WORK
        |
        +---> scan .tasks/ -> unclaimed? -> claim -> resume WORK
        |
        +---> timeout (60s) -> shutdown

    Identity re-injection after compression:
    messages = [identity_block, ...remaining...]
    "You are 'coder', role: backend, team: my-team"

Key insight: "The agent finds work itself."

核心洞察："智能体自己找到工作。"
"""

import json
# 导入 json 模块

import os
# 导入 os 模块

import subprocess
# 导入 subprocess 模块

import threading
# 导入 threading 模块

import time
# 导入 time 模块

import uuid
# 导入 uuid 模块

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
# 团队配置目录

INBOX_DIR = TEAM_DIR / "inbox"
# 收件箱目录

TASKS_DIR = WORKDIR / ".tasks"
# 任务目录（来自 s07）

POLL_INTERVAL = 5
# POLL_INTERVAL：空闲时轮询的间隔时间（秒）

IDLE_TIMEOUT = 60
# IDLE_TIMEOUT：空闲超时时间（秒）

SYSTEM = f"You are a team lead at {WORKDIR}. Teammates are autonomous -- they find work themselves."
# 队长系统提示词

VALID_MSG_TYPES = {
    # 有效消息类型
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}

# -- Request trackers --
# -- 请求追踪器 --
shutdown_requests = {}
plan_requests = {}
_tracker_lock = threading.Lock()
# 关闭和计划请求的追踪器（与 s10 相同）
_claim_lock = threading.Lock()
# _claim_lock：线程锁，保护任务认领的并发访问


# -- MessageBus: JSONL inbox per teammate --
# -- MessageBus（与 s09 相同）--
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)


# -- Task board scanning --
# -- 任务板扫描 --
def scan_unclaimed_tasks() -> list:
    # scan_unclaimed_tasks 函数：扫描未认领的任务
    # 返回值 -> list —— 未认领任务列表
    TASKS_DIR.mkdir(exist_ok=True)
    # 确保任务目录存在
    unclaimed = []
    # 未认领任务列表
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        # 遍历所有任务文件
        task = json.loads(f.read_text())
        # 读取任务
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            # 检查任务是否：
            # - 状态为 pending（待处理）
            # - 没有 owner（未认领）
            # - 没有被阻塞（没有依赖）
            unclaimed.append(task)
            # 添加到未认领列表
    return unclaimed
    # 返回未认领任务列表


def claim_task(task_id: int, owner: str) -> str:
    # claim_task 函数：认领任务
    # 参数 task_id: int —— 任务 ID
    # 参数 owner: str —— 认领者名称
    # 返回值 -> str —— 认领结果
    with _claim_lock:
        # 获取锁，防止并发冲突
        path = TASKS_DIR / f"task_{task_id}.json"
        # 构建任务文件路径
        if not path.exists():
            # 如果任务不存在
            return f"Error: Task {task_id} not found"
            # 返回错误
        task = json.loads(path.read_text())
        # 读取任务
        if task.get("owner"):
            # 如果任务已有 owner
            existing_owner = task.get("owner") or "someone else"
            return f"Error: Task {task_id} has already been claimed by {existing_owner}"
            # 返回错误
        if task.get("status") != "pending":
            # 如果状态不是 pending
            status = task.get("status")
            return f"Error: Task {task_id} cannot be claimed because its status is '{status}'"
            # 返回错误
        if task.get("blockedBy"):
            # 如果有阻塞依赖
            return f"Error: Task {task_id} is blocked by other task(s) and cannot be claimed yet"
            # 返回错误
        task["owner"] = owner
        # 设置 owner
        task["status"] = "in_progress"
        # 更新状态为进行中
        path.write_text(json.dumps(task, indent=2))
        # 保存更新
    return f"Claimed task #{task_id} for {owner}"
    # 返回成功信息


# -- Identity re-injection after compression --
# -- 压缩后的身份重新注入 --
def make_identity_block(name: str, role: str, team_name: str) -> dict:
    # make_identity_block 函数：创建身份块
    # 参数 name: str —— 队友名称
    # 参数 role: str —— 角色
    # 参数 team_name: str —— 团队名称
    # 返回值 -> dict —— 身份消息字典
    return {
        "role": "user",
        "content": f"<identity>You are '{name}', role: {role}, team: {team_name}. Continue your work.</identity>",
    }
    # 返回包含身份信息的消息字典
    # 使用 <identity> XML 标签包裹
    # 这在上下文压缩后特别重要，因为压缩后模型可能忘记自己的身份


# -- Autonomous TeammateManager --
# -- 自主 TeammateManager --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def _set_status(self, name: str, status: str):
        # _set_status 方法：设置队友状态
        # 参数 name: str —— 队友名称
        # 参数 status: str —— 新状态
        member = self._find_member(name)
        # 查找队友
        if member:
            # 如果存在
            member["status"] = status
            # 更新状态
            self._save_config()
            # 保存配置

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(
            target=self._loop,
            args=(name, role, prompt),
            daemon=True,
        )
        # 使用 self._loop 而不是 self._teammate_loop
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _loop(self, name: str, role: str, prompt: str):
        # _loop 方法：队友的主循环，包含 WORK 和 IDLE 两个阶段
        # 参数 name: str —— 队友名称
        # 参数 role: str —— 角色
        # 参数 prompt: str —— 初始提示词
        team_name = self.config["team_name"]
        # 获取团队名称
        sys_prompt = (
            f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
            f"Use idle tool when you have no more work. You will auto-claim new tasks."
        )
        # 队友系统提示词：告知使用 idle 工具表示没有工作
        messages = [{"role": "user", "content": prompt}]
        # 初始化消息历史
        tools = self._teammate_tools()
        # 获取工具列表

        while True:
            # 无限循环：WORK -> IDLE -> WORK -> ...
            # -- WORK PHASE: standard agent loop --
            # -- 工作阶段：标准 Agent 循环 --
            for _ in range(50):
                # 最多 50 轮工作
                inbox = BUS.read_inbox(name)
                # 读取收件箱
                for msg in inbox:
                    # 处理收件箱消息
                    if msg.get("type") == "shutdown_request":
                        # 如果是关闭请求
                        self._set_status(name, "shutdown")
                        # 更新状态为 shutdown
                        return
                        # 结束线程
                    messages.append({"role": "user", "content": json.dumps(msg)})
                    # 追加消息到历史
                try:
                    response = client.messages.create(
                        model=MODEL,
                        system=sys_prompt,
                        messages=messages,
                        tools=tools,
                        max_tokens=8000,
                    )
                except Exception:
                    self._set_status(name, "idle")
                    return
                messages.append({"role": "assistant", "content": response.content})
                # 追加模型回复
                if response.stop_reason != "tool_use":
                    # 如果没有调用工具
                    break
                    # 跳出工作循环，进入 IDLE 阶段
                results = []
                idle_requested = False
                # idle_requested：标记是否请求了 idle
                for block in response.content:
                    if block.type == "tool_use":
                        if block.name == "idle":
                            # 如果调用了 idle 工具
                            idle_requested = True
                            # 标记请求了 idle
                            output = "Entering idle phase. Will poll for new tasks."
                            # 设置输出
                        else:
                            output = self._exec(name, block.name, block.input)
                            # 执行其他工具
                        print(f"  [{name}] {block.name}: {str(output)[:120]}")
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(output),
                        })
                messages.append({"role": "user", "content": results})
                # 追加结果
                if idle_requested:
                    # 如果请求了 idle
                    break
                    # 跳出工作循环，进入 IDLE 阶段

            # -- IDLE PHASE: poll for inbox messages and unclaimed tasks --
            # -- 空闲阶段：轮询收件箱消息和未认领任务 --
            self._set_status(name, "idle")
            # 更新状态为 idle
            resume = False
            # resume：标记是否恢复工作
            polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
            # 计算轮询次数：超时时间 / 轮询间隔
            for _ in range(polls):
                # 轮询多次
                time.sleep(POLL_INTERVAL)
                # 等待轮询间隔
                inbox = BUS.read_inbox(name)
                # 读取收件箱
                if inbox:
                    # 如果有新消息
                    for msg in inbox:
                        # 处理消息
                        if msg.get("type") == "shutdown_request":
                            # 如果是关闭请求
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg)})
                        # 追加消息
                    resume = True
                    # 标记恢复工作
                    break
                    # 跳出轮询
                unclaimed = scan_unclaimed_tasks()
                # 扫描未认领任务
                if unclaimed:
                    # 如果有未认领任务
                    task = unclaimed[0]
                    # 取第一个任务
                    result = claim_task(task["id"], name)
                    # 尝试认领
                    if result.startswith("Error:"):
                        # 如果认领失败（可能已被其他人认领）
                        continue
                        # 继续轮询下一个
                    task_prompt = (
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n"
                        f"{task.get('description', '')}</auto-claimed>"
                    )
                    # 构建任务提示词
                    if len(messages) <= 3:
                        # 如果消息历史很短（可能是上下文压缩后）
                        messages.insert(0, make_identity_block(name, role, team_name))
                        # 在开头插入身份块
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                        # 插入确认消息
                    messages.append({"role": "user", "content": task_prompt})
                    # 追加任务提示词
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    # 追加确认消息
                    resume = True
                    # 标记恢复工作
                    break
                    # 跳出轮询

            if not resume:
                # 如果没有恢复工作（超时）
                self._set_status(name, "shutdown")
                # 更新状态为 shutdown
                return
                # 结束线程
            self._set_status(name, "working")
            # 更新状态为 working，继续工作阶段

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # these base tools are unchanged from s02
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
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2)
        if tool_name == "shutdown_response":
            req_id = args["request_id"]
            with _tracker_lock:
                if req_id in shutdown_requests:
                    shutdown_requests[req_id]["status"] = "approved" if args["approve"] else "rejected"
            BUS.send(
                sender, "lead", args.get("reason", ""),
                "shutdown_response", {"request_id": req_id, "approve": args["approve"]},
            )
            return f"Shutdown {'approved' if args['approve'] else 'rejected'}"
        if tool_name == "plan_approval":
            plan_text = args.get("plan", "")
            req_id = str(uuid.uuid4())[:8]
            with _tracker_lock:
                plan_requests[req_id] = {"from": sender, "plan": plan_text, "status": "pending"}
            BUS.send(
                sender, "lead", plan_text, "plan_approval_response",
                {"request_id": req_id, "plan": plan_text},
            )
            return f"Plan submitted (request_id={req_id}). Waiting for approval."
        if tool_name == "claim_task":
            return claim_task(args["task_id"], sender)
            # claim_task 工具：认领任务
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
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
            {"name": "read_inbox", "description": "Read and drain your inbox.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "shutdown_response", "description": "Respond to a shutdown request.",
             "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}}, "required": ["request_id", "approve"]}},
            {"name": "plan_approval", "description": "Submit a plan for lead approval.",
             "input_schema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
            {"name": "idle", "description": "Signal that you have no more work. Enters idle polling phase.",
             "input_schema": {"type": "object", "properties": {}}},
            # idle 工具：表示没有更多工作，进入空闲轮询阶段
            {"name": "claim_task", "description": "Claim a task from the task board by ID.",
             "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
            # claim_task 工具：从任务板认领任务
        ]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- Base tool implementations (these base tools are unchanged from s02) --
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


# -- Lead-specific protocol handlers --
def handle_shutdown_request(teammate: str) -> str:
    req_id = str(uuid.uuid4())[:8]
    with _tracker_lock:
        shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send(
        "lead", teammate, "Please shut down gracefully.",
        "shutdown_request", {"request_id": req_id},
    )
    return f"Shutdown request {req_id} sent to '{teammate}'"


def handle_plan_review(request_id: str, approve: bool, feedback: str = "") -> str:
    with _tracker_lock:
        req = plan_requests.get(request_id)
    if not req:
        return f"Error: Unknown plan request_id '{request_id}'"
    with _tracker_lock:
        req["status"] = "approved" if approve else "rejected"
    BUS.send(
        "lead", req["from"], feedback, "plan_approval_response",
        {"request_id": request_id, "approve": approve, "feedback": feedback},
    )
    return f"Plan {req['status']} for '{req['from']}'"


def _check_shutdown_status(request_id: str) -> str:
    with _tracker_lock:
        return json.dumps(shutdown_requests.get(request_id, {"error": "not found"}))


# -- Lead tool dispatch (14 tools) --
TOOL_HANDLERS = {
    "bash":              lambda **kw: _run_bash(kw["command"]),
    "read_file":         lambda **kw: _run_read(kw["path"], kw.get("limit")),
    "write_file":        lambda **kw: _run_write(kw["path"], kw["content"]),
    "edit_file":         lambda **kw: _run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "spawn_teammate":    lambda **kw: TEAM.spawn(kw["name"], kw["role"], kw["prompt"]),
    "list_teammates":    lambda **kw: TEAM.list_all(),
    "send_message":      lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":        lambda **kw: json.dumps(BUS.read_inbox("lead"), indent=2),
    "broadcast":         lambda **kw: BUS.broadcast("lead", kw["content"], TEAM.member_names()),
    "shutdown_request":  lambda **kw: handle_shutdown_request(kw["teammate"]),
    "shutdown_response": lambda **kw: _check_shutdown_status(kw.get("request_id", "")),
    "plan_approval":     lambda **kw: handle_plan_review(kw["request_id"], kw["approve"], kw.get("feedback", "")),
    "idle":              lambda **kw: "Lead does not idle.",
    # idle 工具：队长不需要 idle
    "claim_task":        lambda **kw: claim_task(kw["task_id"], "lead"),
    # claim_task 工具：队长也可以认领任务
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
    {"name": "spawn_teammate", "description": "Spawn an autonomous teammate.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "role": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["name", "role", "prompt"]}},
    {"name": "list_teammates", "description": "List all teammates.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "send_message", "description": "Send a message to a teammate.",
     "input_schema": {"type": "object", "properties": {"to": {"type": "string"}, "content": {"type": "string"}, "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)}}, "required": ["to", "content"]}},
    {"name": "read_inbox", "description": "Read and drain the lead's inbox.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "broadcast", "description": "Send a message to all teammates.",
     "input_schema": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
    {"name": "shutdown_request", "description": "Request a teammate to shut down.",
     "input_schema": {"type": "object", "properties": {"teammate": {"type": "string"}}, "required": ["teammate"]}},
    {"name": "shutdown_response", "description": "Check shutdown request status.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
    {"name": "plan_approval", "description": "Approve or reject a teammate's plan.",
     "input_schema": {"type": "object", "properties": {"request_id": {"type": "string"}, "approve": {"type": "boolean"}, "feedback": {"type": "string"}}, "required": ["request_id", "approve"]}},
    {"name": "idle", "description": "Enter idle state (for lead -- rarely used).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim_task", "description": "Claim a task from the board by ID.",
     "input_schema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]


def agent_loop(messages: list):
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2)}</inbox>",
            })
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
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
            query = input("\033[36ms11 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2))
            continue
        if query.strip() == "/tasks":
            TASKS_DIR.mkdir(exist_ok=True)
            for f in sorted(TASKS_DIR.glob("task_*.json")):
                t = json.loads(f.read_text())
                marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
                owner = f" @{t['owner']}" if t.get("owner") else ""
                print(f"  {marker} #{t['id']}: {t['subject']}{owner}")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if hasattr(block, "text"):
                    print(block.text)
        print()
