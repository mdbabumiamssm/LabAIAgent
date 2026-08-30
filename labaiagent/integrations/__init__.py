"""Framework adapters: the same twenty tools in every agent runtime.

Each module is a thin, lazily-importing shim over the gateway registry --
none of them re-implements a tool or adds a second path to hardware.

    labaiagent.integrations.openai_tools     OpenAI chat/Responses tool-calling
    labaiagent.integrations.anthropic_tools  Anthropic Messages tool use
    labaiagent.integrations.langchain_tools  LangChain / LangGraph BaseTool list
    labaiagent.integrations.llamaindex_tools LlamaIndex FunctionTool list
    labaiagent.integrations.crewai_tools     CrewAI BaseTool list
    labaiagent.integrations.autogen_tools    AutoGen/AG2-registerable callables
    labaiagent.integrations.smolagents_tools HF smolagents Tool list
    labaiagent.integrations.base             plain typed Python callables

The frameworks themselves are optional dependencies; importing an adapter
raises a clear message naming the missing package.
"""

from .base import make_callables

__all__ = ["make_callables"]
