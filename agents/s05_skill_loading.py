#!/usr/bin/env python3
# 指定解释器为 python3。

# Harness: on-demand knowledge -- domain expertise, loaded when the model asks.
# Harness：按需加载知识 —— 领域专业知识，仅在模型请求时加载。
# 本文件引入 SkillLoader，实现两层技能注入机制，避免系统提示词膨胀。
# 第一层（低成本）：在系统提示词中仅列出技能名称和描述。
# 第二层（按需）：在工具结果中返回技能的完整内容。

"""
s05_skill_loading.py - Skills

s05_skill_loading.py - 技能加载

Two-layer skill injection that avoids bloating the system prompt:

避免系统提示词膨胀的两层技能注入机制：

    Layer 1 (cheap): skill names in system prompt (~100 tokens/skill)
    Layer 2 (on demand): full skill body in tool_result

    skills/
      pdf/
        SKILL.md          <-- frontmatter (name, description) + body
      code-review/
        SKILL.md

    System prompt:
    +--------------------------------------+
    | You are a coding agent.              |
    | Skills available:                    |
    |   - pdf: Process PDF files...        |  <-- Layer 1: metadata only
    |   - code-review: Review code...      |
    +--------------------------------------+

    When model calls load_skill("pdf"):
    +--------------------------------------+
    | tool_result:                         |
    | <skill>                              |
    |   Full PDF processing instructions   |  <-- Layer 2: full body
    |   Step 1: ...                        |
    |   Step 2: ...                        |
    | </skill>                             |
    +--------------------------------------+

Key insight: "Don't put everything in the system prompt. Load on demand."

核心洞察："不要把所有内容都放进系统提示词中。按需加载。"
"""

import os
# 导入 os 模块。

import re
# 导入 re 模块：正则表达式，用于解析 YAML frontmatter。

import subprocess
# 导入 subprocess 模块。

import yaml
# 导入 yaml 模块：用于解析 YAML 格式的 frontmatter。

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

SKILLS_DIR = WORKDIR / "skills"
# 技能目录路径：工作目录下的 skills 文件夹。
# 每个技能是一个子目录（如 pdf、code-review），包含 SKILL.md 文件。


# -- SkillLoader: scan skills/<name>/SKILL.md with YAML frontmatter --
# -- SkillLoader：扫描 skills/<名称>/SKILL.md 文件，解析 YAML frontmatter --
class SkillLoader:
    # SkillLoader 类：扫描技能目录，解析技能的元数据和正文内容。
    def __init__(self, skills_dir: Path):
        # 构造函数。
        # 参数 skills_dir: Path —— 技能目录的 Path 对象。
        self.skills_dir = skills_dir
        # 保存技能目录路径。
        self.skills = {}
        # self.skills：字典，存储所有已加载的技能。
        # 键是技能名称，值是包含 meta（元数据）、body（正文）和 path（路径）的字典。
        self._load_all()
        # 在初始化时自动加载所有技能。

    def _load_all(self):
        # _load_all 方法：扫描技能目录，加载所有技能。
        if not self.skills_dir.exists():
            # 检查技能目录是否存在。
            return
            # 不存在则直接返回，不加载任何技能。
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            # 递归遍历技能目录下的所有 SKILL.md 文件。
            # sorted()：按字母顺序排序，保证加载顺序一致。
            # rglob("SKILL.md")：递归查找所有名为 SKILL.md 的文件。
            text = f.read_text()
            # 读取 SKILL.md 文件的完整文本内容。
            meta, body = self._parse_frontmatter(text)
            # 解析 frontmatter，分离元数据和正文。
            name = meta.get("name", f.parent.name)
            # 获取技能名称：优先从元数据中的 name 字段获取，
            # 如果不存在，使用所在目录名（f.parent.name）作为默认名称。
            self.skills[name] = {"meta": meta, "body": body, "path": str(f)}
            # 将技能信息保存到字典中：
            # - "meta": 解析后的元数据字典。
            # - "body": 技能的正文内容（不含 frontmatter）。
            # - "path": 技能文件的字符串路径。

    def _parse_frontmatter(self, text: str) -> tuple:
        # _parse_frontmatter 方法：解析 Markdown 文件中的 YAML frontmatter。
        # YAML frontmatter 是 Markdown 文件顶部的元数据区域，格式为：
        # ---
        # name: pdf
        # description: Process PDF files
        # ---
        # （正文内容）
        # 参数 text: str —— SKILL.md 文件的完整文本。
        # 返回值 -> tuple —— 返回 (元数据字典, 正文字符串) 的元组。
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        # 使用正则表达式匹配 frontmatter：
        # - ^---\n：匹配开头的三个短横线（frontmatter 开始标记）和换行。
        # - (.*?)：非贪婪匹配 frontmatter 内容（YAML 部分），捕获为元数据。
        # - \n---\n：匹配结束的三个短横线和换行。
        # - (.*)：匹配剩余所有内容（正文部分），使用 re.DOTALL 让 . 匹配换行符。
        if not match:
            # 如果没有匹配到 frontmatter（文件格式不正确）。
            return {}, text
            # 返回空元数据字典和完整文本作为正文。
        try:
            meta = yaml.safe_load(match.group(1)) or {}
            # 使用 yaml.safe_load() 解析 YAML 内容：
            # - match.group(1)：提取第一个捕获组（frontmatter 的 YAML 内容）。
            # - yaml.safe_load()：安全地解析 YAML，不支持任意代码执行。
            # - or {}：如果解析结果为 None（空 YAML），使用空字典。
        except yaml.YAMLError:
            # 捕获 YAML 解析错误。
            meta = {}
            # 解析失败则使用空字典。
        return meta, match.group(2).strip()
        # 返回元数据字典和正文内容（去除首尾空白）。

    def get_descriptions(self) -> str:
        # get_descriptions 方法：生成技能描述列表（第一层，用于系统提示词）。
        # 返回值 -> str —— 格式化的技能描述字符串。
        if not self.skills:
            # 如果没有加载任何技能。
            return "(no skills available)"
            # 返回提示信息。
        lines = []
        # lines：存储描述行的列表。
        for name, skill in self.skills.items():
            # 遍历所有技能。
            desc = skill["meta"].get("description", "No description")
            # 从元数据中获取描述，如果不存在则使用默认描述。
            tags = skill["meta"].get("tags", "")
            # 从元数据中获取标签（可选）。
            line = f"  - {name}: {desc}"
            # 格式化技能描述行：名称 + 描述。
            if tags:
                # 如果有标签。
                line += f" [{tags}]"
                # 追加标签信息。
            lines.append(line)
            # 添加到列表。
        return "\n".join(lines)
        # 用换行符连接所有行返回。

    def get_content(self, name: str) -> str:
        # get_content 方法：获取技能的完整内容（第二层，用于工具结果）。
        # 参数 name: str —— 技能名称。
        # 返回值 -> str —— 技能的完整内容，包装在 XML 标签中。
        skill = self.skills.get(name)
        # 从字典中查找指定名称的技能。
        if not skill:
            # 如果技能不存在。
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
            # 返回错误信息，列出所有可用技能。
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"
        # 返回包装在 XML 标签中的技能正文：
        # - <skill name="...">：XML 开始标签，包含技能名称。
        # - skill['body']：技能的完整正文内容。
        # - </skill>：XML 结束标签。
        # 使用 XML 标签方便模型识别技能内容的边界。


SKILL_LOADER = SkillLoader(SKILLS_DIR)
# 创建全局 SkillLoader 实例：在模块加载时自动扫描并加载所有技能。

# Layer 1: skill metadata injected into system prompt
# 第一层：技能元数据注入到系统提示词中。
SYSTEM = f"""You are a coding agent at {WORKDIR}.
Use load_skill to access specialized knowledge before tackling unfamiliar topics.

Skills available:
{SKILL_LOADER.get_descriptions()}"""
# 系统提示词：
# - 告知模型当前工作目录。
# - 指示在处理不熟悉的主题前使用 load_skill 工具加载专业知识。
# - 注入所有可用技能的描述列表（第一层，低成本，仅元数据）。
# 这样模型知道有哪些技能可用，但不需要在每次请求时都加载完整的技能内容。


# -- Tool implementations --
# -- 工具实现 --
def safe_path(p: str) -> Path:
    # 安全路径检查。
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    # 执行 bash 命令。
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
    # 读取文件。
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    # 写入文件。
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    # 编辑文件。
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
    # 工具调度映射表，在 s02 基础上增加了 load_skill 工具。
    "bash":       lambda **kw: run_bash(kw["command"]),
    "read_file":  lambda **kw: run_read(kw["path"], kw.get("limit")),
    "write_file": lambda **kw: run_write(kw["path"], kw["content"]),
    "edit_file":  lambda **kw: run_edit(kw["path"], kw["old_text"], kw["new_text"]),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    # load_skill 工具：加载指定名称的技能完整内容。
    # - kw["name"]：技能名称。
    # - SKILL_LOADER.get_content()：获取技能的完整正文。
}

TOOLS = [
    # 可用工具列表，增加了 load_skill。
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "load_skill", "description": "Load specialized knowledge by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string", "description": "Skill name to load"}}, "required": ["name"]}},
    # load_skill 工具定义：
    # - "description": "按名称加载专业知识"。
    # - "input_schema": 输入参数。
    #   - "name": 技能名称，字符串类型，必填。
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
            query = input("\033[36ms05 >> \033[0m")
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