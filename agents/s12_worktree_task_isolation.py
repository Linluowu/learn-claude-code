#!/usr/bin/env python3
# 指定脚本解释器为 python3，shebang 行用于类 Unix 系统直接执行脚本

# Harness: directory isolation -- parallel execution lanes that never collide.
# Harness：目录隔离 —— 永远不会冲突的并行执行通道。
# 本文件引入 git worktree 机制，通过目录级别隔离实现并行任务执行。
# 任务是控制平面（control plane），worktree 是执行平面（execution plane）。

"""
s12_worktree_task_isolation.py - Worktree + Task Isolation

s12_worktree_task_isolation.py - Worktree + 任务隔离

Directory-level isolation for parallel task execution.
Tasks are the control plane and worktrees are the execution plane.

用于并行任务执行的目录级别隔离。
任务是控制平面，worktree 是执行平面。

    .tasks/task_12.json
      {
        "id": 12,
        "subject": "Implement auth refactor",
        "status": "in_progress",
        "worktree": "auth-refactor"
      }

    .worktrees/index.json
      {
        "worktrees": [
          {
            "name": "auth-refactor",
            "path": ".../.worktrees/auth-refactor",
            "branch": "wt/auth-refactor",
            "task_id": 12,
            "status": "active"
          }
        ]
      }

Key insight: "Isolate by directory, coordinate by task ID."

核心洞察："通过目录隔离，通过任务 ID 协调。"
"""

import json
# 导入 json 模块：用于序列化和反序列化 JSON 数据

import os
# 导入 os 模块：用于环境变量操作和路径处理

import re
# 导入 re 模块：用于正则表达式匹配，验证 worktree 名称格式

import subprocess
# 导入 subprocess 模块：用于执行外部命令（git 命令、shell 命令）

import time
# 导入 time 模块：用于生成时间戳，记录任务和 worktree 的创建/更新时间

from pathlib import Path
# 从 pathlib 导入 Path 类：用于面向对象的路径操作

from anthropic import Anthropic
# 从 anthropic 包导入 Anthropic 类：Anthropic 官方 SDK，用于与 Claude API 通信

from dotenv import load_dotenv
# 从 python-dotenv 导入 load_dotenv：用于从 .env 文件加载环境变量

load_dotenv(override=True)
# 加载 .env 文件中的环境变量到 os.environ
# override=True 表示如果环境变量已存在，用 .env 中的值覆盖

if os.getenv("ANTHROPIC_BASE_URL"):
    # 检查是否设置了自定义 Anthropic API 基础 URL（如代理服务器地址）
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
    # 如果使用了自定义 base URL，从环境变量中移除认证令牌
    # 原因：自定义代理可能不需要或不能使用 auth token

WORKDIR = Path.cwd()
# 设置 WORKDIR 为当前工作目录（Current Working Directory）
# Path.cwd() 返回当前进程的工作目录的 Path 对象

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
# 创建 Anthropic API 客户端实例
# base_url 参数从环境变量读取，未设置则使用默认值（官方 API 地址）

MODEL = os.environ["MODEL_ID"]
# 从环境变量读取 MODEL_ID，指定要使用的 Claude 模型版本


def detect_repo_root(cwd: Path) -> Path | None:
    # detect_repo_root 函数：检测当前目录是否在 git 仓库中，如果是则返回仓库根目录
    # 参数 cwd: Path —— 要检测的目录路径
    # 返回值 -> Path | None —— git 仓库根目录，如果不在仓库中则返回 None
    """Return git repo root if cwd is inside a repo, else None."""
    # 文档字符串：如果在仓库内则返回 git 仓库根目录，否则返回 None
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            # 执行 git rev-parse --show-toplevel 命令获取仓库根目录
            # --show-toplevel 选项返回当前 git 仓库的顶层目录
            cwd=cwd,
            # 在 cwd 目录下执行命令
            capture_output=True,
            # 捕获标准输出和标准错误
            text=True,
            # 以文本模式返回输出
            timeout=10,
            # 10 秒超时
        )
        if r.returncode != 0:
            # 如果命令返回非零退出码，说明不在 git 仓库中
            return None
            # 返回 None
        root = Path(r.stdout.strip())
        # 从命令输出中提取仓库根目录路径，去除首尾空白字符
        return root if root.exists() else None
        # 如果路径存在则返回，否则返回 None
    except Exception:
        # 捕获所有异常（git 未安装、权限错误等）
        return None
        # 返回 None


REPO_ROOT = detect_repo_root(WORKDIR) or WORKDIR
# 检测 git 仓库根目录，如果不在 git 仓库中则使用当前工作目录
# REPO_ROOT 是 git worktree 操作的基础目录

SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Use task + worktree tools for multi-task work. "
    "For parallel or risky changes: create tasks, allocate worktree lanes, "
    "run commands in those lanes, then choose keep/remove for closeout. "
    "Use worktree_events when you need lifecycle visibility."
)
# 系统提示词：告知模型当前工作目录
# 指示使用 task + worktree 工具进行多任务工作
# 建议为并行或风险性更改创建任务并分配 worktree 通道
# 在需要生命周期可见性时使用 worktree_events 工具


# -- EventBus: append-only lifecycle events for observability --
# -- EventBus：仅追加的生命周期事件，用于可观测性 --
class EventBus:
    # EventBus 类：记录 worktree 和任务的生命周期事件
    def __init__(self, event_log_path: Path):
        # 构造函数
        # 参数 event_log_path: Path —— 事件日志文件路径
        self.path = event_log_path
        # 保存事件日志文件路径
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 创建日志文件的父目录（如果不存在）
        # parents=True 递归创建所有不存在的父目录
        if not self.path.exists():
            # 如果日志文件不存在
            self.path.write_text("")
            # 创建空文件

    def emit(
        self,
        event: str,
        task: dict | None = None,
        worktree: dict | None = None,
        error: str | None = None,
    ):
        # emit 方法：发出一个生命周期事件
        # 参数 event: str —— 事件名称（如 "worktree.create.before"）
        # 参数 task: dict | None —— 相关的任务信息，可选
        # 参数 worktree: dict | None —— 相关的 worktree 信息，可选
        # 参数 error: str | None —— 错误信息，可选
        payload = {
            "event": event,
            # 事件名称
            "ts": time.time(),
            # 时间戳（Unix 时间戳，秒）
            "task": task or {},
            # 任务信息，默认为空字典
            "worktree": worktree or {},
            # worktree 信息，默认为空字典
        }
        if error:
            # 如果提供了错误信息
            payload["error"] = error
            # 添加到 payload 中
        with self.path.open("a", encoding="utf-8") as f:
            # 以追加模式打开日志文件
            f.write(json.dumps(payload) + "\n")
            # 将事件序列化为 JSON 并写入文件，每条事件一行（JSONL 格式）

    def list_recent(self, limit: int = 20) -> str:
        # list_recent 方法：列出最近的事件
        # 参数 limit: int = 20 —— 最多返回多少条事件，默认 20
        # 返回值 -> str —— 格式化的事件列表 JSON 字符串
        n = max(1, min(int(limit or 20), 200))
        # 规范化 limit 参数：
        # - int(limit or 20)：如果 limit 为 None 或空则使用默认值 20
        # - min(..., 200)：最大不超过 200
        # - max(1, ...)：最小不低于 1
        lines = self.path.read_text(encoding="utf-8").splitlines()
        # 读取日志文件内容并按行分割
        recent = lines[-n:]
        # 取最后 n 行（最近的事件）
        items = []
        # 解析后的事件列表
        for line in recent:
            try:
                items.append(json.loads(line))
                # 解析 JSON
            except Exception:
                items.append({"event": "parse_error", "raw": line})
                # 解析失败则记录解析错误
        return json.dumps(items, indent=2)
        # 将事件列表格式化为 JSON 字符串返回


# -- TaskManager: persistent task board with optional worktree binding --
# -- TaskManager：支持可选 worktree 绑定的持久化任务板 --
class TaskManager:
    # TaskManager 类：管理持久化任务，支持与 worktree 的绑定
    def __init__(self, tasks_dir: Path):
        # 构造函数
        # 参数 tasks_dir: Path —— 任务存储目录
        self.dir = tasks_dir
        # 保存任务目录
        self.dir.mkdir(parents=True, exist_ok=True)
        # 创建任务目录（如果不存在）
        self._next_id = self._max_id() + 1
        # 初始化下一个任务 ID

    def _max_id(self) -> int:
        # _max_id 方法：查找当前最大任务 ID
        # 返回值 -> int —— 最大任务 ID
        ids = []
        # ID 列表
        for f in self.dir.glob("task_*.json"):
            # 遍历所有任务文件
            try:
                ids.append(int(f.stem.split("_")[1]))
                # 提取 ID
            except Exception:
                # 如果提取失败则跳过
                pass
        return max(ids) if ids else 0
        # 返回最大 ID，如果没有任务则返回 0

    def _path(self, task_id: int) -> Path:
        # _path 方法：构建任务文件路径
        # 参数 task_id: int —— 任务 ID
        # 返回值 -> Path —— 文件路径
        return self.dir / f"task_{task_id}.json"

    def _load(self, task_id: int) -> dict:
        # _load 方法：加载指定任务
        # 参数 task_id: int —— 任务 ID
        # 返回值 -> dict —— 任务字典
        path = self._path(task_id)
        # 获取文件路径
        if not path.exists():
            # 如果文件不存在
            raise ValueError(f"Task {task_id} not found")
        return json.loads(path.read_text())
        # 读取并解析 JSON

    def _save(self, task: dict):
        # _save 方法：保存任务到文件
        # 参数 task: dict —— 任务字典
        self._path(task["id"]).write_text(json.dumps(task, indent=2))
        # 序列化为 JSON 并写入文件

    def create(self, subject: str, description: str = "") -> str:
        # create 方法：创建新任务
        # 参数 subject: str —— 任务主题
        # 参数 description: str = "" —— 任务描述
        # 返回值 -> str —— 新任务的 JSON 字符串
        task = {
            "id": self._next_id,
            # 任务 ID
            "subject": subject,
            # 任务主题
            "description": description,
            # 任务描述
            "status": "pending",
            # 初始状态为 pending（待处理）
            "owner": "",
            # 初始无负责人
            "worktree": "",
            # 初始未绑定 worktree
            "blockedBy": [],
            # 初始无阻塞依赖
            "created_at": time.time(),
            # 创建时间戳
            "updated_at": time.time(),
            # 更新时间戳（初始与创建时间相同）
        }
        self._save(task)
        # 保存任务
        self._next_id += 1
        # ID 自增
        return json.dumps(task, indent=2)
        # 返回 JSON 字符串

    def get(self, task_id: int) -> str:
        # get 方法：获取任务详情
        # 参数 task_id: int —— 任务 ID
        # 返回值 -> str —— 任务 JSON 字符串
        return json.dumps(self._load(task_id), indent=2)

    def exists(self, task_id: int) -> bool:
        # exists 方法：检查任务是否存在
        # 参数 task_id: int —— 任务 ID
        # 返回值 -> bool —— 是否存在
        return self._path(task_id).exists()

    def update(self, task_id: int, status: str = None, owner: str = None) -> str:
        # update 方法：更新任务状态或负责人
        # 参数 task_id: int —— 任务 ID
        # 参数 status: str = None —— 新状态，可选
        # 参数 owner: str = None —— 新负责人，可选
        # 返回值 -> str —— 更新后的任务 JSON 字符串
        task = self._load(task_id)
        # 加载任务
        if status:
            # 如果提供了新状态
            if status not in ("pending", "in_progress", "completed"):
                # 检查状态是否有效
                raise ValueError(f"Invalid status: {status}")
            task["status"] = status
            # 更新状态
        if owner is not None:
            # 如果提供了新负责人（包括空字符串）
            task["owner"] = owner
            # 更新负责人
        task["updated_at"] = time.time()
        # 更新更新时间戳
        self._save(task)
        # 保存
        return json.dumps(task, indent=2)
        # 返回更新后的 JSON

    def bind_worktree(self, task_id: int, worktree: str, owner: str = "") -> str:
        # bind_worktree 方法：将任务绑定到 worktree
        # 参数 task_id: int —— 任务 ID
        # 参数 worktree: str —— worktree 名称
        # 参数 owner: str = "" —— 负责人，可选
        # 返回值 -> str —— 更新后的任务 JSON 字符串
        task = self._load(task_id)
        # 加载任务
        task["worktree"] = worktree
        # 设置 worktree 名称
        if owner:
            # 如果提供了负责人
            task["owner"] = owner
            # 更新负责人
        if task["status"] == "pending":
            # 如果状态为 pending
            task["status"] = "in_progress"
            # 自动更新为 in_progress（进行中）
        task["updated_at"] = time.time()
        # 更新更新时间戳
        self._save(task)
        # 保存
        return json.dumps(task, indent=2)

    def unbind_worktree(self, task_id: int) -> str:
        # unbind_worktree 方法：解除任务与 worktree 的绑定
        # 参数 task_id: int —— 任务 ID
        # 返回值 -> str —— 更新后的任务 JSON 字符串
        task = self._load(task_id)
        # 加载任务
        task["worktree"] = ""
        # 清空 worktree 名称
        task["updated_at"] = time.time()
        # 更新更新时间戳
        self._save(task)
        # 保存
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        # list_all 方法：列出所有任务
        # 返回值 -> str —— 格式化的任务列表字符串
        tasks = []
        # 任务列表
        for f in sorted(self.dir.glob("task_*.json")):
            # 遍历所有任务文件
            tasks.append(json.loads(f.read_text()))
            # 读取并解析
        if not tasks:
            # 如果没有任务
            return "No tasks."
            # 返回提示
        lines = []
        # 输出行列表
        for t in tasks:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }.get(t["status"], "[?]")
            # 根据状态选择标记
            owner = f" owner={t['owner']}" if t.get("owner") else ""
            # 如果有负责人则显示
            wt = f" wt={t['worktree']}" if t.get("worktree") else ""
            # 如果绑定了 worktree 则显示
            lines.append(f"{marker} #{t['id']}: {t['subject']}{owner}{wt}")
            # 格式化输出行
        return "\n".join(lines)
        # 返回格式化列表


TASKS = TaskManager(REPO_ROOT / ".tasks")
# 创建全局 TaskManager 实例，任务存储在仓库根目录的 .tasks 文件夹中

EVENTS = EventBus(REPO_ROOT / ".worktrees" / "events.jsonl")
# 创建全局 EventBus 实例，事件日志存储在 .worktrees/events.jsonl


# -- WorktreeManager: create/list/run/remove git worktrees + lifecycle index --
# -- WorktreeManager：创建/列出/运行/移除 git worktree + 生命周期索引 --
class WorktreeManager:
    # WorktreeManager 类：管理 git worktree 的创建、列出、运行命令和移除
    def __init__(self, repo_root: Path, tasks: TaskManager, events: EventBus):
        # 构造函数
        # 参数 repo_root: Path —— git 仓库根目录
        # 参数 tasks: TaskManager —— 任务管理器
        # 参数 events: EventBus —— 事件总线
        self.repo_root = repo_root
        # 保存仓库根目录
        self.tasks = tasks
        # 保存任务管理器
        self.events = events
        # 保存事件总线
        self.dir = repo_root / ".worktrees"
        # worktree 存储目录：仓库根目录下的 .worktrees 文件夹
        self.dir.mkdir(parents=True, exist_ok=True)
        # 创建目录（如果不存在）
        self.index_path = self.dir / "index.json"
        # worktree 索引文件路径
        if not self.index_path.exists():
            # 如果索引文件不存在
            self.index_path.write_text(json.dumps({"worktrees": []}, indent=2))
            # 创建空索引
        self.git_available = self._is_git_repo()
        # 检测是否在 git 仓库中

    def _is_git_repo(self) -> bool:
        # _is_git_repo 方法：检测当前目录是否在 git 仓库中
        # 返回值 -> bool —— 是否在 git 仓库中
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                # 执行 git rev-parse --is-inside-work-tree 命令
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
            # 如果返回码为 0，则在 git 仓库中
        except Exception:
            # 捕获所有异常
            return False
            # 返回 False

    def _run_git(self, args: list[str]) -> str:
        # _run_git 方法：执行 git 命令
        # 参数 args: list[str] —— git 命令参数列表
        # 返回值 -> str —— 命令输出
        if not self.git_available:
            # 如果不在 git 仓库中
            raise RuntimeError("Not in a git repository. worktree tools require git.")
            # 抛出错误
        r = subprocess.run(
            ["git", *args],
            # 构造完整命令：git + 参数
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode != 0:
            # 如果命令失败
            msg = (r.stdout + r.stderr).strip()
            # 合并 stdout 和 stderr
            raise RuntimeError(msg or f"git {' '.join(args)} failed")
            # 抛出错误
        return (r.stdout + r.stderr).strip() or "(no output)"
        # 返回输出

    def _load_index(self) -> dict:
        # _load_index 方法：加载 worktree 索引
        # 返回值 -> dict —— 索引字典
        return json.loads(self.index_path.read_text())

    def _save_index(self, data: dict):
        # _save_index 方法：保存 worktree 索引
        # 参数 data: dict —— 索引数据
        self.index_path.write_text(json.dumps(data, indent=2))

    def _find(self, name: str) -> dict | None:
        # _find 方法：查找指定名称的 worktree
        # 参数 name: str —— worktree 名称
        # 返回值 -> dict | None —— worktree 信息，如果不存在返回 None
        idx = self._load_index()
        # 加载索引
        for wt in idx.get("worktrees", []):
            # 遍历所有 worktree
            if wt.get("name") == name:
                # 找到匹配的
                return wt
                # 返回信息
        return None
        # 未找到则返回 None

    def _validate_name(self, name: str):
        # _validate_name 方法：验证 worktree 名称格式
        # 参数 name: str —— 要验证的名称
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", name or ""):
            # 使用正则表达式验证：
            # - 只允许字母、数字、点、下划线和连字符
            # - 长度 1-40 个字符
            raise ValueError(
                "Invalid worktree name. Use 1-40 chars: letters, numbers, ., _, -"
            )
            # 验证失败则抛出错误

    def create(self, name: str, task_id: int = None, base_ref: str = "HEAD") -> str:
        # create 方法：创建新的 git worktree
        # 参数 name: str —— worktree 名称
        # 参数 task_id: int = None —— 可选，要绑定的任务 ID
        # 参数 base_ref: str = "HEAD" —— 基于哪个 git 引用创建，默认 HEAD
        # 返回值 -> str —— 新 worktree 的 JSON 字符串
        self._validate_name(name)
        # 验证名称格式
        if self._find(name):
            # 如果 worktree 已存在
            raise ValueError(f"Worktree '{name}' already exists in index")
        if task_id is not None and not self.tasks.exists(task_id):
            # 如果指定了任务 ID 但任务不存在
            raise ValueError(f"Task {task_id} not found")

        path = self.dir / name
        # worktree 目录路径
        branch = f"wt/{name}"
        # 分支名称格式：wt/<name>
        self.events.emit(
            "worktree.create.before",
            task={"id": task_id} if task_id is not None else {},
            worktree={"name": name, "base_ref": base_ref},
        )
        # 发出创建前事件
        try:
            self._run_git(["worktree", "add", "-b", branch, str(path), base_ref])
            # 执行 git worktree add 命令创建 worktree：
            # - -b branch：创建并切换到新分支
            # - path：worktree 目录
            # - base_ref：基于的引用

            entry = {
                "name": name,
                "path": str(path),
                "branch": branch,
                "task_id": task_id,
                "status": "active",
                "created_at": time.time(),
            }
            # 构建索引条目

            idx = self._load_index()
            # 加载索引
            idx["worktrees"].append(entry)
            # 添加新条目
            self._save_index(idx)
            # 保存索引

            if task_id is not None:
                # 如果指定了任务 ID
                self.tasks.bind_worktree(task_id, name)
                # 绑定任务到 worktree

            self.events.emit(
                "worktree.create.after",
                task={"id": task_id} if task_id is not None else {},
                worktree={
                    "name": name,
                    "path": str(path),
                    "branch": branch,
                    "status": "active",
                },
            )
            # 发出创建后事件
            return json.dumps(entry, indent=2)
            # 返回 JSON
        except Exception as e:
            # 如果创建失败
            self.events.emit(
                "worktree.create.failed",
                task={"id": task_id} if task_id is not None else {},
                worktree={"name": name, "base_ref": base_ref},
                error=str(e),
            )
            # 发出失败事件
            raise
            # 重新抛出异常

    def list_all(self) -> str:
        # list_all 方法：列出所有 worktree
        # 返回值 -> str —— 格式化的 worktree 列表
        idx = self._load_index()
        # 加载索引
        wts = idx.get("worktrees", [])
        # 获取 worktree 列表
        if not wts:
            # 如果没有 worktree
            return "No worktrees in index."
            # 返回提示
        lines = []
        # 输出行列表
        for wt in wts:
            suffix = f" task={wt['task_id']}" if wt.get("task_id") else ""
            # 如果绑定了任务则显示
            lines.append(
                f"[{wt.get('status', 'unknown')}] {wt['name']} -> "
                f"{wt['path']} ({wt.get('branch', '-')}){suffix}"
            )
            # 格式化输出行
        return "\n".join(lines)
        # 返回格式化列表

    def status(self, name: str) -> str:
        # status 方法：查看指定 worktree 的 git 状态
        # 参数 name: str —— worktree 名称
        # 返回值 -> str —— git status 输出
        wt = self._find(name)
        # 查找 worktree
        if not wt:
            # 如果不存在
            return f"Error: Unknown worktree '{name}'"
            # 返回错误
        path = Path(wt["path"])
        # 获取路径
        if not path.exists():
            # 如果路径不存在
            return f"Error: Worktree path missing: {path}"
            # 返回错误
        r = subprocess.run(
            ["git", "status", "--short", "--branch"],
            # 执行 git status --short --branch
            # --short：简洁格式
            # --branch：显示分支信息
            cwd=path,
            # 在 worktree 目录中执行
            capture_output=True,
            text=True,
            timeout=60,
        )
        text = (r.stdout + r.stderr).strip()
        # 合并 stdout 和 stderr
        return text or "Clean worktree"
        # 如果有输出则返回，否则返回 "Clean worktree"

    def run(self, name: str, command: str) -> str:
        # run 方法：在指定 worktree 中运行 shell 命令
        # 参数 name: str —— worktree 名称
        # 参数 command: str —— 要执行的命令
        # 返回值 -> str —— 命令输出
        dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
        if any(d in command for d in dangerous):
            # 安全检查
            return "Error: Dangerous command blocked"

        wt = self._find(name)
        # 查找 worktree
        if not wt:
            # 如果不存在
            return f"Error: Unknown worktree '{name}'"
            # 返回错误
        path = Path(wt["path"])
        # 获取路径
        if not path.exists():
            # 如果路径不存在
            return f"Error: Worktree path missing: {path}"
            # 返回错误

        try:
            r = subprocess.run(
                command,
                shell=True,
                # 通过 shell 执行
                cwd=path,
                # 在 worktree 目录中执行
                capture_output=True,
                text=True,
                timeout=300,
                # 5 分钟超时
            )
            out = (r.stdout + r.stderr).strip()
            # 合并 stdout 和 stderr
            return out[:50000] if out else "(no output)"
            # 返回输出（限制 50000 字符）
        except subprocess.TimeoutExpired:
            # 超时
            return "Error: Timeout (300s)"

    def remove(self, name: str, force: bool = False, complete_task: bool = False) -> str:
        # remove 方法：移除 worktree
        # 参数 name: str —— worktree 名称
        # 参数 force: bool = False —— 是否强制移除
        # 参数 complete_task: bool = False —— 是否将绑定的任务标记为已完成
        # 返回值 -> str —— 移除结果
        wt = self._find(name)
        # 查找 worktree
        if not wt:
            # 如果不存在
            return f"Error: Unknown worktree '{name}'"
            # 返回错误

        self.events.emit(
            "worktree.remove.before",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={"name": name, "path": wt.get("path")},
        )
        # 发出移除前事件
        try:
            args = ["worktree", "remove"]
            # 构造 git 命令
            if force:
                # 如果强制移除
                args.append("--force")
                # 添加 --force 选项
            args.append(wt["path"])
            # 添加路径
            self._run_git(args)
            # 执行 git worktree remove

            if complete_task and wt.get("task_id") is not None:
                # 如果要求完成任务且绑定了任务
                task_id = wt["task_id"]
                before = json.loads(self.tasks.get(task_id))
                # 保存任务之前的状态
                self.tasks.update(task_id, status="completed")
                # 更新任务状态为已完成
                self.tasks.unbind_worktree(task_id)
                # 解除绑定
                self.events.emit(
                    "task.completed",
                    task={
                        "id": task_id,
                        "subject": before.get("subject", ""),
                        "status": "completed",
                    },
                    worktree={"name": name},
                )
                # 发出任务完成事件

            idx = self._load_index()
            # 加载索引
            for item in idx.get("worktrees", []):
                # 遍历所有 worktree
                if item.get("name") == name:
                    # 找到要移除的
                    item["status"] = "removed"
                    # 更新状态为 removed
                    item["removed_at"] = time.time()
                    # 记录移除时间
            self._save_index(idx)
            # 保存索引

            self.events.emit(
                "worktree.remove.after",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path"), "status": "removed"},
            )
            # 发出移除后事件
            return f"Removed worktree '{name}'"
            # 返回成功信息
        except Exception as e:
            # 如果移除失败
            self.events.emit(
                "worktree.remove.failed",
                task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
                worktree={"name": name, "path": wt.get("path")},
                error=str(e),
            )
            # 发出失败事件
            raise
            # 重新抛出

    def keep(self, name: str) -> str:
        # keep 方法：保留 worktree（不移除，但标记为保留状态）
        # 参数 name: str —— worktree 名称
        # 返回值 -> str —— 结果 JSON 字符串
        wt = self._find(name)
        # 查找 worktree
        if not wt:
            # 如果不存在
            return f"Error: Unknown worktree '{name}'"
            # 返回错误

        idx = self._load_index()
        # 加载索引
        kept = None
        # 保留的条目
        for item in idx.get("worktrees", []):
            # 遍历
            if item.get("name") == name:
                # 找到
                item["status"] = "kept"
                # 更新状态为 kept
                item["kept_at"] = time.time()
                # 记录保留时间
                kept = item
                # 保存引用
        self._save_index(idx)
        # 保存索引

        self.events.emit(
            "worktree.keep",
            task={"id": wt.get("task_id")} if wt.get("task_id") is not None else {},
            worktree={
                "name": name,
                "path": wt.get("path"),
                "status": "kept",
            },
        )
        # 发出保留事件
        return json.dumps(kept, indent=2) if kept else f"Error: Unknown worktree '{name}'"
        # 返回结果


WORKTREES = WorktreeManager(REPO_ROOT, TASKS, EVENTS)
# 创建全局 WorktreeManager 实例


# -- Base tools (kept minimal, same style as previous sessions) --
# -- 基础工具（保持最小化，与之前会话相同的风格）--
def safe_path(p: str) -> Path:
    # 安全路径函数：确保路径不会逃逸出工作目录
    path = (WORKDIR / p).resolve()
    # 解析为绝对路径
    if not path.is_relative_to(WORKDIR):
        # 检查是否在工作目录内
        raise ValueError(f"Path escapes workspace: {p}")
        # 逃逸则抛出错误
    return path
    # 返回安全路径


def run_bash(command: str) -> str:
    # run_bash 函数：执行 bash 命令
    # 参数 command: str —— 要执行的命令
    # 返回值 -> str —— 命令输出
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    # 危险命令黑名单
    if any(d in command for d in dangerous):
        # 安全检查
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            # 通过 shell 执行
            cwd=WORKDIR,
            # 在工作目录中执行
            capture_output=True,
            text=True,
            timeout=120,
            # 2 分钟超时
        )
        out = (r.stdout + r.stderr).strip()
        # 合并 stdout 和 stderr
        return out[:50000] if out else "(no output)"
        # 返回输出
    except subprocess.TimeoutExpired:
        # 超时
        return "Error: Timeout (120s)"


def run_read(path: str, limit: int = None) -> str:
    # run_read 函数：读取文件内容
    # 参数 path: str —— 文件路径
    # 参数 limit: int = None —— 行数限制
    # 返回值 -> str —— 文件内容
    try:
        lines = safe_path(path).read_text().splitlines()
        # 安全读取并按行分割
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
        return f"Wrote {len(content)} bytes"
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


TOOL_HANDLERS = {
    # 工具处理函数字典
    "bash": lambda **kw: run_bash(kw["command"]),
    "read_file": lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file": lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "task_create": lambda **kw: TASKS.create(kw["subject"], kw.get("description", "")),
    # task_create 工具：创建新任务
    "task_list": lambda **kw: TASKS.list_all(),
    # task_list 工具：列出所有任务
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
    # task_get 工具：获取任务详情
    "task_update": lambda **kw: TASKS.update(kw["task_id"], kw.get("status"), kw.get("owner")),
    # task_update 工具：更新任务
    "task_bind_worktree": lambda **kw: TASKS.bind_worktree(kw["task_id"], kw["worktree"], kw.get("owner", "")),
    # task_bind_worktree 工具：将任务绑定到 worktree
    "worktree_create": lambda **kw: WORKTREES.create(kw["name"], kw.get("task_id"), kw.get("base_ref", "HEAD")),
    # worktree_create 工具：创建 worktree
    "worktree_list": lambda **kw: WORKTREES.list_all(),
    # worktree_list 工具：列出所有 worktree
    "worktree_status": lambda **kw: WORKTREES.status(kw["name"]),
    # worktree_status 工具：查看 worktree 状态
    "worktree_run": lambda **kw: WORKTREES.run(kw["name"], kw["command"]),
    # worktree_run 工具：在 worktree 中运行命令
    "worktree_keep": lambda **kw: WORKTREES.keep(kw["name"]),
    # worktree_keep 工具：保留 worktree
    "worktree_remove": lambda **kw: WORKTREES.remove(kw["name"], kw.get("force", False), kw.get("complete_task", False)),
    # worktree_remove 工具：移除 worktree
    "worktree_events": lambda **kw: EVENTS.list_recent(kw.get("limit", 20)),
    # worktree_events 工具：查看最近事件
}

TOOLS = [
    # 可用工具列表
    {
        "name": "bash",
        "description": "Run a shell command in the current workspace (blocking).",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "task_create",
        "description": "Create a new task on the shared task board.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["subject"],
        },
    },
    {
        "name": "task_list",
        "description": "List all tasks with status, owner, and worktree binding.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "task_get",
        "description": "Get task details by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "task_update",
        "description": "Update task status or owner.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed"],
                },
                "owner": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_bind_worktree",
        "description": "Bind a task to a worktree name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "worktree": {"type": "string"},
                "owner": {"type": "string"},
            },
            "required": ["task_id", "worktree"],
        },
    },
    {
        "name": "worktree_create",
        "description": "Create a git worktree and optionally bind it to a task.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "task_id": {"type": "integer"},
                "base_ref": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "worktree_list",
        "description": "List worktrees tracked in .worktrees/index.json.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "worktree_status",
        "description": "Show git status for one worktree.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_run",
        "description": "Run a shell command in a named worktree directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "command": {"type": "string"},
            },
            "required": ["name", "command"],
        },
    },
    {
        "name": "worktree_remove",
        "description": "Remove a worktree and optionally mark its bound task completed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "force": {"type": "boolean"},
                "complete_task": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "worktree_keep",
        "description": "Mark a worktree as kept in lifecycle state without removing it.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "worktree_events",
        "description": "List recent worktree/task lifecycle events from .worktrees/events.jsonl.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
]


def agent_loop(messages: list):
    # agent_loop 函数：Agent 核心循环
    # 参数 messages: list —— 消息历史列表
    while True:
        # 无限循环
        response = client.messages.create(
            # 调用 Claude API
            model=MODEL,
            # 使用的模型
            system=SYSTEM,
            # 系统提示词
            messages=messages,
            # 消息历史
            tools=TOOLS,
            # 可用工具
            max_tokens=8000,
            # 最大 token 数
        )
        messages.append({"role": "assistant", "content": response.content})
        # 追加模型回复到消息历史
        if response.stop_reason != "tool_use":
            # 如果模型没有调用工具
            return
            # 结束循环

        results = []
        # 工具结果列表
        for block in response.content:
            # 遍历模型回复中的每个内容块
            if block.type == "tool_use":
                # 如果是工具调用
                handler = TOOL_HANDLERS.get(block.name)
                # 查找对应的处理函数
                try:
                    output = handler(**block.input) if handler else f"Unknown tool: {block.name}"
                    # 执行工具调用
                except Exception as e:
                    # 捕获异常
                    output = f"Error: {e}"
                    # 返回错误信息
                print(f"> {block.name}:")
                # 打印工具名称
                print(str(output)[:200])
                # 打印输出（前 200 字符）
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
                # 构建工具结果
        messages.append({"role": "user", "content": results})
        # 将工具结果作为用户消息追加到对话历史


if __name__ == "__main__":
    # 当脚本直接运行时执行
    print(f"Repo root for s12: {REPO_ROOT}")
    # 打印仓库根目录
    if not WORKTREES.git_available:
        # 如果不在 git 仓库中
        print("Note: Not in a git repo. worktree_* tools will return errors.")
        # 打印提示

    history = []
    # 初始化空的消息历史
    while True:
        # 无限循环
        try:
            query = input("\033[36ms12 >> \033[0m")
            # 显示青色提示符 "s12 >> "，接收用户输入
        except (EOFError, KeyboardInterrupt):
            # 捕获 EOF 或中断
            break
            # 退出循环
        if query.strip().lower() in ("q", "exit", ""):
            # 检查退出命令
            break
            # 退出循环
        history.append({"role": "user", "content": query})
        # 将用户输入追加到消息历史
        agent_loop(history)
        # 启动 Agent 循环
        response_content = history[-1]["content"]
        # 获取最后一条消息的内容
        if isinstance(response_content, list):
            # 如果内容是列表
            for block in response_content:
                # 遍历每个内容块
                if hasattr(block, "text"):
                    # 如果有 text 属性（文本块）
                    print(block.text)
                    # 打印文本内容
        print()
        # 打印空行分隔