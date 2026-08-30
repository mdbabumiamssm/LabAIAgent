"""The Agent Gateway -- one tool registry, every agent runtime.

The registry defines the fixed tool surface once. Adapters render it
mechanically for each protocol and framework:

  - MCP (stdio and HTTP)                       labaiagent.mcp.server
  - REST + OpenAPI 3.1                         labaiagent.gateway.rest
  - OpenAI / Anthropic / Gemini tool schemas   labaiagent.gateway.schemas
  - LangChain, LlamaIndex, CrewAI, AutoGen,
    smolagents, plain callables                labaiagent.integrations.*

Every adapter funnels into ``dispatch`` -> ``LabSession.call``. None of them
adds a second path to hardware.
"""

from .auth import Authenticator, Principal, Role
from .events import EventBus
from .registry import TOOLS, GatewayContext, ToolSpec, dispatch, tool_index

__all__ = ["GatewayContext", "ToolSpec", "TOOLS", "dispatch", "tool_index",
           "Principal", "Authenticator", "Role", "EventBus"]
