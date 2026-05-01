# agents/ - Harness implementations (s01-s12) + full reference (s_full)
# 本目录包含 Harness（ harness 是"套具/框架"的意思，这里指支撑 AI Agent 运行的基础设施代码）的实现，
# 从 s01 到 s12 共12个教学示例文件，以及一个完整的参考实现 s_full。

# Each file is self-contained and runnable: python agents/s01_agent_loop.py
# 每个文件都是自包含且可独立运行的，可以直接用 python agents/s01_agent_loop.py 执行。

# The model is the agent. These files are the harness.
# "模型"本身才是 Agent（智能体），而这些文件只是支撑模型运行的" harness / 套具"。
# 也就是说，真正的智能来自大语言模型（LLM），Python 代码只是搭建了一个循环和工具调用的框架，
# 让模型能够持续接收用户输入、思考、调用工具、观察结果，直到任务完成。
