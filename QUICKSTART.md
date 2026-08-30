# LabAIAgent — 5-minute start

## 1. Install
    pip install -e .

## 2. See it work (no hardware needed)
    python examples/01_bca_assay.py           # 4-instrument protein assay
    python examples/02_qpcr_and_safety.py     # qPCR + every safety layer tripped
    python examples/03_add_new_instrument.py  # onboarding a novel instrument
    python examples/04_universal_agents.py    # one lab, every agent runtime
    python examples/05_knowledge_and_oversight.py  # PubMed → workflow, e-sign, oversight
    python examples/06_memory_and_recovery.py     # restart continuity + recovery
    python -m pytest tests/ -q                # 196 tests

## 3. Inspect the built-in lab
    labaiagent drivers
    labaiagent doctor   --lab config/example_lab.yaml
    labaiagent describe --lab config/example_lab.yaml reader

## 4. Add YOUR first real instrument
    labaiagent scaffold thermo.varioskan --category plate_reader --transport filewatch
      # → edit the generated file: fill in _connect/_disconnect/_halt + capabilities
    labaiagent verify thermo_varioskan.py:ThermoVarioskan --level strict
      # → add a 5-line stanza to your lab YAML

Then repeat. Run `labaiagent doctor` after each one.

## 5. Connect Claude (MCP over stdio)
    labaiagent serve --lab config/example_lab.yaml --readonly    # start here
    labaiagent serve --lab config/example_lab.yaml --dry-run     # then this
    labaiagent serve --lab config/example_lab.yaml               # only then this

Claude Desktop config:

    {
      "mcpServers": {
        "lab": {
          "command": "labaiagent",
          "args": ["serve", "--lab", "/abs/path/config/example_lab.yaml", "--readonly"]
        }
      }
    }

## 6. Connect everything else (HTTP gateway)
    cp config/principals.yaml my_principals.yaml   # set real API keys!
    labaiagent serve --lab config/example_lab.yaml --http --port 8859 \
                     --auth my_principals.yaml

    One port serves REST (POST /tools/{name}), OpenAPI 3.1 (/openapi.json),
    MCP over HTTP (POST /mcp), and live events (GET /events, SSE).

    # OpenAI / Gemini / Anthropic native tool schemas, no server required:
    labaiagent schemas --format openai
    labaiagent schemas --format gemini
    labaiagent schemas --format anthropic

    # Framework shims (LangChain, LlamaIndex, CrewAI, AutoGen, smolagents):
    from labaiagent.integrations.langchain_tools import get_tools

    # Remote Python client:
    from labaiagent.client import LabClient
    lab = LabClient("http://lab-pc:8859", api_key="lak_...")
    lab.read("reader", "read_count")

## Recommended rollout order
readonly → dry-run → live with `autonomy_ceiling: low` → raise to `medium`
only once the audit log shows you what the agent actually does. Give every
agent its own API key so the audit trail, rate limits, and ceilings are
per-identity.
