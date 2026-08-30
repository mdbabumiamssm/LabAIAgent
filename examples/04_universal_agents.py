"""One lab, every agent runtime -- the universality demo.

The same simulated lab is driven four ways, with zero server-side changes
between them:

  A. Native tool schemas exported for OpenAI, Anthropic and Gemini
  B. Plain Python callables (AutoGen / custom loops / REPL)
  C. A live HTTP gateway with API keys, roles, and per-actor ceilings,
     driven through the LabClient SDK -- including an async job
  D. MCP over HTTP -- the same JSON-RPC a remote MCP client would speak

No LLM API keys are needed: this exercises the *interfaces* agents use, with
the agent's decisions scripted. Wire a real model in by handing it the
schemas from step A (or the MCP server) and executing its tool calls with
labaiagent.integrations.*.

Run:  python examples/04_universal_agents.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import LabSession
from labaiagent.client import LabClient, ToolFailed
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    World,
)
from labaiagent.gateway import schemas
from labaiagent.gateway.auth import Authenticator
from labaiagent.gateway.rest import GatewayServer
from labaiagent.integrations import anthropic_tools, make_callables, openai_tools


def rule(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def build_lab() -> tuple[LabSession, World]:
    world = World(seed=4)
    session = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world),
         SimulatedRobotArm("arm", world=world)],
        name="universal-demo", audit_path="runs/universal_audit.jsonl",
        actor="user:babu",
    )
    session.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    return session, world


def main() -> int:
    session, _ = build_lab()

    # ---------------------------------------------------------------- A
    rule("A. One registry -> every vendor's tool dialect")
    for label, export in (("OpenAI", schemas.to_openai_tools()),
                          ("Anthropic", schemas.to_anthropic_tools()),
                          ("Gemini", schemas.to_gemini_tools())):
        n = (len(export[0]["function_declarations"])
             if label == "Gemini" else len(export))
        print(f"  {label:<10} {n} tools exported")
    print("  (also: labaiagent schemas --format openai | anthropic | gemini "
          "| openapi)")

    # ---------------------------------------------------------------- B
    rule("B. Plain callables -- AutoGen, custom loops, a REPL")
    fns = {f.__name__: f for f in make_callables(session)}
    out = fns["read_state"](device_id="lh", capability="flow_rate")
    print(f"  read_state(lh, flow_rate) -> {out['result']['value']} uL/s")

    # A scripted 'model turn' through the OpenAI and Anthropic executors:
    oa_msg = openai_tools.execute_tool_call(session, {
        "id": "call_1", "type": "function",
        "function": {"name": "snapshot", "arguments": "{}"}})
    print(f"  OpenAI executor    -> role={oa_msg['role']}, "
          f"ok={json.loads(oa_msg['content'])['ok']}")
    an_res = anthropic_tools.execute_tool_use(session, {
        "type": "tool_use", "id": "tu_1", "name": "list_devices", "input": {}})
    print(f"  Anthropic executor -> tool_result, is_error={an_res['is_error']}")

    # ---------------------------------------------------------------- C
    rule("C. HTTP gateway: API keys, roles, per-actor ceilings, async jobs")
    auth = Authenticator.from_config({"principals": [
        {"id": "agent:demo", "role": "operator", "api_key": "demo-key",
         "autonomy_ceiling": "medium"},
        {"id": "agent:watcher", "role": "observer", "api_key": "watch-key"},
    ]})
    server = GatewayServer(session, host="127.0.0.1", port=0, auth=auth).start()
    print(f"  gateway up at {server.url} (OpenAPI at /openapi.json)")

    lab = LabClient(server.url, api_key="demo-key")
    print(f"  operator reads flow_rate     -> {lab.read('lh', 'flow_rate')}")
    lab.write("lh", "flow_rate", value=80.0, reason="demo")
    print(f"  operator writes flow_rate=80 -> {lab.read('lh', 'flow_rate')}")

    watcher = LabClient(server.url, api_key="watch-key")
    try:
        watcher.write("lh", "flow_rate", value=10.0)
    except ToolFailed as exc:
        print(f"  observer tries to write      -> {exc.payload['error']} "
              f"(role boundary held)")

    job = lab.call("run_procedure", device_id="lh", capability="transfer",
                   arguments={"source_barcode": "P1", "source_well": "A1",
                              "dest_barcode": "P1", "dest_well": "B1",
                              "volume": 25.0},
                   reason="async demo", mode="async")
    done = lab.wait_job(job["result"]["job_id"], timeout=30)
    print(f"  async transfer job           -> {done['state']}, delivered "
          f"{done['result']['delivered_uL']} uL")

    # ---------------------------------------------------------------- D
    rule("D. MCP over HTTP -- what a remote MCP client speaks")
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/list"}).encode()
    req = urllib.request.Request(
        server.url + "/mcp", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer demo-key"})
    with urllib.request.urlopen(req) as r:
        tools = json.loads(r.read())["result"]["tools"]
    print(f"  POST /mcp tools/list -> {len(tools)} tools "
          f"(same registry, same safety engine)")

    server.stop()
    s = session.audit.summary()
    print(f"\n  Everything above is in one audit chain: {s['records']} records, "
          f"{s['chain_status']}")
    session.disconnect_all()
    print("\nSame fourteen tools. Same safety engine. Every agent runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
