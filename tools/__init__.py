"""
工具包。

目前只有搜索工具；K8s 相关工具由 MCP Server 动态提供（见 agent.py），
不需要在这里手写。新增自定义工具的约定：在本包内建模块，暴露一个
返回 list[BaseTool] 的函数，再由 agent.py 汇总。
"""
