"""Tests for the universal Agent Gateway: registry, jobs, auth, schemas,
REST server, MCP-over-HTTP, framework shims, and the robustness fixes.

Run:  python -m pytest tests/test_gateway.py -q
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from labaiagent import LabSession, Risk
from labaiagent.client import LabClient, ToolFailed
from labaiagent.core.audit import AuditLog
from labaiagent.core.types import convert
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    SimulatedThermocycler,
    World,
)
from labaiagent.gateway import schemas
from labaiagent.gateway.auth import Authenticator, Principal, Role
from labaiagent.gateway.registry import (
    TOOL_SPECS,
    GatewayContext,
    dispatch,
    tool_index,
)
from labaiagent.gateway.rest import GatewayServer
from labaiagent.integrations import anthropic_tools as an
from labaiagent.integrations import make_callables
from labaiagent.integrations import openai_tools as oa
from labaiagent.mcp.server import handle_jsonrpc

# ==========================================================================
# Fixtures
# ==========================================================================

@pytest.fixture
def world():
    return World(seed=99)


@pytest.fixture
def session(world, tmp_path):
    s = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world),
         SimulatedThermocycler("cycler", world=world),
         SimulatedRobotArm("arm", world=world)],
        name="gw-test", audit_path=tmp_path / "audit.jsonl", actor="user:test",
    )
    s.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    yield s
    s.disconnect_all()


@pytest.fixture
def auth():
    return Authenticator.from_config({
        "principals": [
            {"id": "agent:op", "role": "operator", "api_key": "key-op"},
            {"id": "agent:ro", "role": "observer", "api_key": "key-ro"},
            {"id": "user:approver", "kind": "human", "role": "approver",
             "api_key": "key-appr"},
            {"id": "agent:tight", "role": "operator", "api_key": "key-tight",
             "autonomy_ceiling": "low"},
        ],
        "allow_anonymous": False,
    })


@pytest.fixture
def server(session, auth):
    srv = GatewayServer(session, host="127.0.0.1", port=0, auth=auth).start()
    yield srv
    srv.stop()


# ==========================================================================
# Registry & dispatch
# ==========================================================================

class TestRegistry:
    def test_every_tool_has_schema_and_role(self):
        for t in TOOL_SPECS:
            assert t.input_schema["type"] == "object"
            assert t.description
            json.dumps(t.input_schema)   # serialisable

    def test_dispatch_accepts_bare_session(self, session):
        out = dispatch(session, "list_devices", {})
        assert out["ok"] and out["result"]["count"] == 4

    def test_context_is_cached_per_session(self, session):
        a = GatewayContext.for_session(session)
        b = GatewayContext.for_session(session)
        assert a is b

    def test_readonly_hides_actuating_tools(self):
        names = set(tool_index(readonly_only=True))
        assert "read_state" in names and "write_state" not in names

    def test_observer_role_cannot_actuate(self, session):
        ro = Principal(id="agent:ro", role=Role.OBSERVER)
        out = dispatch(session, "write_state",
                       {"device_id": "lh", "capability": "flow_rate",
                        "arguments": {"value": 50}}, principal=ro)
        assert out["ok"] is False and out["error"] == "forbidden"

    def test_observer_can_emergency_stop(self, session):
        ro = Principal(id="agent:ro", role=Role.OBSERVER)
        out = dispatch(session, "emergency_stop", {"reason": "t"}, principal=ro)
        assert out["ok"] and out["result"]["stopped"]

    def test_agent_cannot_mint_its_own_approval(self, session):
        op = Principal(id="agent:op", role=Role.OPERATOR)
        out = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "self-serve"}, principal=op)
        assert out["ok"] is False and out["error"] == "forbidden"

    def test_approver_token_unlocks_high_risk(self, session, world):
        appr = Principal(id="user:appr", role=Role.APPROVER)
        out = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "test"}, principal=appr)
        assert out["ok"]
        token = out["result"]["approval"]
        out2 = dispatch(session, "run_procedure",
                        {"device_id": "arm", "capability": "move_labware",
                         "arguments": {"barcode": "P1",
                                       "destination": "hotel_1"},
                         "approval": token})
        assert out2["ok"], out2

    def test_no_traceback_in_error_payloads(self, session):
        out = dispatch(session, "describe_device", {"device_id": "ghost"})
        assert out["ok"] is False
        assert "traceback" not in out

    def test_actor_ceiling_restricts_below_session(self, session):
        session.safety.actor_ceilings["agent:tight"] = Risk.LOW
        tight = Principal(id="agent:tight", role=Role.OPERATOR)
        # flow_rate write is LOW risk -> allowed
        ok = dispatch(session, "write_state",
                      {"device_id": "lh", "capability": "flow_rate",
                       "arguments": {"value": 60}}, principal=tight)
        assert ok["ok"], ok
        # transfer is MEDIUM risk -> above this actor's ceiling
        out = dispatch(session, "run_procedure",
                       {"device_id": "lh", "capability": "transfer",
                        "arguments": {"source_barcode": "P1",
                                      "source_well": "A1",
                                      "dest_barcode": "P1", "dest_well": "B1",
                                      "volume": 10.0}}, principal=tight)
        assert out["ok"] is False and out["error"] == "ConfirmationRequired"


# ==========================================================================
# Jobs
# ==========================================================================

class TestJobs:
    def test_async_procedure_returns_job(self, session):
        out = dispatch(session, "run_procedure",
                       {"device_id": "lh", "capability": "transfer",
                        "arguments": {"source_barcode": "P1",
                                      "source_well": "A1",
                                      "dest_barcode": "P1", "dest_well": "B1",
                                      "volume": 20.0},
                        "mode": "async"})
        assert out["ok"]
        job_id = out["result"]["job_id"]
        ctx = GatewayContext.for_session(session)
        job = ctx.jobs.wait(job_id, timeout=10)
        assert job.state.value == "succeeded"
        assert job.result["delivered_uL"] > 0

    def test_job_failure_is_captured_not_raised(self, session):
        out = dispatch(session, "run_procedure",
                       {"device_id": "lh", "capability": "transfer",
                        "arguments": {"source_barcode": "GHOST",
                                      "source_well": "A1",
                                      "dest_barcode": "P1", "dest_well": "B1",
                                      "volume": 20.0},
                        "mode": "async"})
        job_id = out["result"]["job_id"]
        ctx = GatewayContext.for_session(session)
        job = ctx.jobs.wait(job_id, timeout=10)
        assert job.state.value == "failed" and "GHOST" in (job.error or "")

    def test_long_sync_procedure_is_refused_with_guidance(self, session):
        # run_qpcr declares est_duration_s=5400 -> sync must refuse.
        out = dispatch(session, "run_procedure",
                       {"device_id": "cycler", "capability": "run_qpcr"})
        assert out["ok"] is False
        assert "async" in out["message"]

    def test_protocol_as_job_with_progress_and_get_job(self, session):
        proto = {"name": "p", "steps": [
            {"name": f"s{i}", "device": "lh",
             "capability": "read:tips_available"} for i in range(4)]}
        out = dispatch(session, "run_protocol",
                       {"protocol": proto, "mode": "async"})
        job_id = out["result"]["job_id"]
        ctx = GatewayContext.for_session(session)
        ctx.jobs.wait(job_id, timeout=10)
        st = dispatch(session, "get_job", {"job_id": job_id})
        assert st["ok"] and st["result"]["state"] == "succeeded"
        assert st["result"]["progress"]["steps_total"] == 4

    def test_cancel_job_stops_between_steps(self, session):
        proto = {"name": "slow", "steps": [
            {"name": f"s{i}", "device": "lh", "capability": "proc:transfer",
             "args": {"source_barcode": "P1", "source_well": "A1",
                      "dest_barcode": "P1", "dest_well": "B1",
                      "volume": 1.0}} for i in range(50)]}
        out = dispatch(session, "run_protocol",
                       {"protocol": proto, "mode": "async"})
        job_id = out["result"]["job_id"]
        dispatch(session, "cancel_job", {"job_id": job_id})
        ctx = GatewayContext.for_session(session)
        job = ctx.jobs.wait(job_id, timeout=15)
        assert job.state.value == "cancelled"

    def test_job_events_are_audited(self, session):
        out = dispatch(session, "write_state",
                       {"device_id": "lh", "capability": "flow_rate",
                        "arguments": {"value": 55}, "mode": "async"})
        ctx = GatewayContext.for_session(session)
        ctx.jobs.wait(out["result"]["job_id"], timeout=10)
        events = [r.event for r in session.audit.records()]
        assert "job_submitted" in events and "job_finished" in events


# ==========================================================================
# Schemas
# ==========================================================================

class TestSchemas:
    def test_openai_export_shape(self):
        tools = schemas.to_openai_tools()
        assert all(t["type"] == "function" for t in tools)
        names = {t["function"]["name"] for t in tools}
        assert {"list_devices", "write_state", "get_job"} <= names

    def test_anthropic_export_shape(self):
        tools = schemas.to_anthropic_tools()
        assert all("input_schema" in t for t in tools)

    def test_gemini_export_shape(self):
        decls = schemas.to_gemini_tools()[0]["function_declarations"]
        assert any(d["name"] == "run_protocol" for d in decls)

    def test_readonly_export_has_no_actuators(self):
        names = {t["name"] for t in schemas.to_anthropic_tools(readonly_only=True)}
        assert "write_state" not in names and "read_state" in names

    def test_openapi_document(self):
        doc = schemas.to_openapi()
        assert doc["openapi"] == "3.1.0"
        assert "/tools/write_state" in doc["paths"]
        json.dumps(doc)

    def test_exports_are_deep_copies(self):
        a = schemas.to_openai_tools()
        a[0]["function"]["parameters"]["properties"]["INJECTED"] = {}
        b = schemas.to_openai_tools()
        assert "INJECTED" not in b[0]["function"]["parameters"]["properties"]


# ==========================================================================
# REST gateway end-to-end (real HTTP over loopback)
# ==========================================================================

class TestRestGateway:
    def test_routes_require_auth(self, server):
        # /health stays open (liveness probes), but only as a bare pulse;
        # every other route without a key is 401.
        req = urllib.request.Request(server.url + "/tools")
        try:
            urllib.request.urlopen(req)
            pytest.fail("expected 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

    def test_client_end_to_end(self, server):
        lab = LabClient(server.url, api_key="key-op")
        assert lab.health()["ok"]
        assert {"list_devices", "run_protocol"} <= {t["name"] for t in lab.tools()}
        assert lab.read("lh", "flow_rate") == 100.0
        out = lab.write("lh", "flow_rate", value=42.0, reason="e2e")
        assert out["result"] == 42.0
        assert lab.read("lh", "flow_rate") == 42.0

    def test_observer_key_is_forbidden_from_writes(self, server):
        lab = LabClient(server.url, api_key="key-ro")
        with pytest.raises(ToolFailed) as exc:
            lab.write("lh", "flow_rate", value=50.0)
        assert exc.value.payload["error"] == "forbidden"

    def test_bad_key_is_unauthorized(self, server):
        lab = LabClient(server.url, api_key="wrong")
        out = lab.call("list_devices")
        assert out["error"] == "unauthorized"

    def test_safety_violation_passes_through_structured(self, server):
        lab = LabClient(server.url, api_key="key-op")
        out = lab.call("write_state", device_id="arm", capability="speed",
                       arguments={"value": 9999})
        assert out["ok"] is False and out["error"] == "LimitViolation"
        assert "permitted" in out

    def test_async_job_over_rest(self, server):
        lab = LabClient(server.url, api_key="key-op")
        out = lab.call("run_procedure", device_id="lh", capability="transfer",
                       arguments={"source_barcode": "P1", "source_well": "A1",
                                  "dest_barcode": "P1", "dest_well": "C1",
                                  "volume": 15.0}, mode="async")
        job = lab.wait_job(out["result"]["job_id"], timeout=15)
        assert job["state"] == "succeeded"

    def test_openapi_served(self, server):
        lab = LabClient(server.url, api_key="key-op")
        doc = lab.openapi()
        assert "/tools/run_protocol" in doc["paths"]

    def test_mcp_over_http(self, server):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": "tools/list"}).encode()
        req = urllib.request.Request(
            server.url + "/mcp", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer key-op"})
        with urllib.request.urlopen(req) as r:
            out = json.loads(r.read())
        names = {t["name"] for t in out["result"]["tools"]}
        assert "run_protocol" in names

    def test_refuses_nonloopback_without_auth(self, session):
        from labaiagent.core.errors import ConfigurationError
        with pytest.raises(ConfigurationError):
            GatewayServer(session, host="0.0.0.0", port=0, auth=None)


# ==========================================================================
# MCP resources (stdio protocol handler)
# ==========================================================================

class TestMCPResources:
    def test_resources_list_and_read(self, session):
        out = handle_jsonrpc(session, {"jsonrpc": "2.0", "id": 1,
                                       "method": "resources/list"})
        uris = {r["uri"] for r in out["result"]["resources"]}
        assert "lab://manifest" in uris
        out = handle_jsonrpc(session, {"jsonrpc": "2.0", "id": 2,
                                       "method": "resources/read",
                                       "params": {"uri": "lab://reference"}})
        assert "INSTRUMENT" in out["result"]["contents"][0]["text"]


# ==========================================================================
# Framework shims (no framework installs required)
# ==========================================================================

class TestFrameworkShims:
    def test_plain_callables(self, session):
        fns = {f.__name__: f for f in make_callables(session)}
        out = fns["read_state"](device_id="lh", capability="flow_rate")
        assert out["ok"] and out["result"]["value"] == 100.0

    def test_openai_execute_tool_call(self, session):
        call = {"id": "call_1", "type": "function",
                "function": {"name": "snapshot", "arguments": "{}"}}
        msg = oa.execute_tool_call(session, call)
        assert msg["role"] == "tool" and msg["tool_call_id"] == "call_1"
        assert json.loads(msg["content"])["ok"]

    def test_anthropic_execute_tool_use(self, session):
        block = {"type": "tool_use", "id": "tu_1", "name": "list_devices",
                 "input": {}}
        res = an.execute_tool_use(session, block)
        assert res["tool_use_id"] == "tu_1" and res["is_error"] is False

    def test_anthropic_error_marks_is_error(self, session):
        block = {"type": "tool_use", "id": "tu_2", "name": "write_state",
                 "input": {"device_id": "arm", "capability": "speed",
                           "arguments": {"value": 9999}}}
        res = an.execute_tool_use(session, block)
        assert res["is_error"] is True


# ==========================================================================
# Robustness fixes
# ==========================================================================

class TestFixes:
    def test_unit_conversions_added(self):
        assert convert(1.0, "mg/mL", "ug/mL") == pytest.approx(1000.0)
        assert convert(1.0, "ng/uL", "ug/mL") == pytest.approx(1.0)
        assert convert(60.0, "mL/min", "uL/s") == pytest.approx(1000.0)
        assert convert(1.0, "bar", "kPa") == pytest.approx(100.0)
        with pytest.raises(ValueError):
            convert(1.0, "mM", "mg/mL")   # needs a molar mass -- must refuse

    def test_audit_refuses_to_extend_tampered_log(self, tmp_path):
        path = tmp_path / "a.jsonl"
        log = AuditLog(path)
        log.record("invoke", device="d")
        log.record("result", device="d")
        lines = path.read_text().splitlines()
        rec = json.loads(lines[0])
        rec["device"] = "tampered"
        lines[0] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n")
        resumed = AuditLog(path)               # readable...
        ok, _ = resumed.verify()
        assert not ok
        with pytest.raises(RuntimeError):      # ...but not extendable
            resumed.record("invoke", device="d")

    def test_snapshot_is_audited(self, session):
        session.snapshot()
        assert any(r.event == "snapshot" for r in session.audit.records())

    def test_rate_limits_are_per_actor(self, session):
        session.safety.set_rate_limit("lh.flow_rate", max_calls=2, window_s=60)
        session.write("lh", "flow_rate", value=10.0, actor="agent:a")
        session.write("lh", "flow_rate", value=11.0, actor="agent:a")
        # agent:a is now exhausted; agent:b is not.
        session.write("lh", "flow_rate", value=12.0, actor="agent:b")
        from labaiagent.core.errors import SafetyViolation
        with pytest.raises(SafetyViolation, match="Rate limit"):
            session.write("lh", "flow_rate", value=13.0, actor="agent:a")

    def test_estop_reset_reverifies_devices(self, session):
        # Break one device's self-test, e-stop, reset: it must land in ERROR.
        dev = session.get("arm")
        dev._self_test = lambda: False           # type: ignore[method-assign]
        session.emergency_stop("test")
        session.reset_emergency_stop(operator="op", reason="clear")
        assert session.get("arm").state.value == "error"
        assert session.get("lh").state.value == "idle"

    def test_protocol_cancellation_marks_skipped(self, session):
        from labaiagent.orchestration.workflow import Protocol
        p = Protocol("c")
        for i in range(5):
            p.step(f"s{i}", "lh", "read:tips_available")
        calls = {"n": 0}

        def cancel_after_two() -> bool:
            return calls["n"] >= 1

        def progress(step) -> None:
            calls["n"] += 1

        summary = p.run(session, progress=progress,
                        should_cancel=cancel_after_two)
        assert summary["cancelled"] is True
        assert summary["counts"].get("skipped", 0) >= 1

    def test_filewatch_timeout_is_not_retried_as_transient(self, tmp_path):
        from labaiagent.core.errors import TransientError, TransportError
        from labaiagent.transports.concrete import FileWatchTransport
        fw = FileWatchTransport(watch_dir=tmp_path, pattern="*.csv",
                                timeout=0.2, poll_s=0.05)
        fw.open()
        with pytest.raises(TransportError) as exc:
            fw.await_result(timeout=0.2)
        assert not isinstance(exc.value, TransientError)

    def test_conformance_catches_low_end_limit_bypass(self):
        from labaiagent import Device, Param, Range, read, write

        class SneakyDevice(Device):
            vendor, model, category = "t", "t", "generic"
            notes = "test"

            def _connect(self):
                pass

            def _disconnect(self):
                pass

            @read("x", unit="degC")
            def get_x(self) -> float:
                return 1.0

            @write("x", params=[Param("value", float, "degC",
                                      limits=Range(10.0, 20.0, "degC"))])
            def set_x(self, value: float) -> None:
                pass

        dev = SneakyDevice("t1")
        # Sabotage: swap the limit for one that only checks the high end.
        cap = dev._capabilities["write:x"]

        class HighOnly(Range):
            def check(self, value):
                if value > self.high:
                    raise ValueError("too high")

        p = cap.params[0]
        p.limits = (HighOnly(10.0, 20.0, "degC"),)
        from labaiagent.core.conformance import verify_driver
        rep = verify_driver(dev, exercise=True)   # limit probing is dynamic
        assert any(f.check == "limits" for f in rep.errors)


# ==========================================================================
# Regression tests for the pre-release adversarial review findings
# ==========================================================================

class TestReviewFixes:
    def test_protocol_cannot_shed_actor_ceiling(self, session):
        """R#1: wrapping an over-ceiling actuation in a protocol must not
        evade the submitter's per-actor autonomy ceiling."""
        session.safety.actor_ceilings["agent:tight"] = Risk.LOW
        tight = Principal(id="agent:tight", role=Role.OPERATOR)
        proto = {"name": "smuggle", "steps": [
            {"name": "s", "device": "lh", "capability": "proc:transfer",
             "args": {"source_barcode": "P1", "source_well": "A1",
                      "dest_barcode": "P1", "dest_well": "B1",
                      "volume": 10.0}}]}
        out = dispatch(session, "run_protocol", {"protocol": proto},
                       principal=tight)
        # Static validation must already flag the missing approval under the
        # actor's EFFECTIVE ceiling (low), exactly as a direct call would.
        assert out["ok"] and out["result"]["valid"] is False
        assert any("approval" in p for p in out["result"]["problems"])

    def test_protocol_steps_audit_the_verified_actor(self, session):
        op = Principal(id="agent:op", role=Role.OPERATOR)
        proto = {"name": "attrib", "steps": [
            {"name": "s", "device": "lh", "capability": "read:tips_available"}]}
        out = dispatch(session, "run_protocol", {"protocol": proto},
                       principal=op)
        assert out["ok"], out
        invokes = [r for r in session.audit.records()
                   if r.event == "invoke" and r.capability == "tips_available"]
        assert invokes and invokes[-1].actor == "agent:op"

    def test_protocol_job_carries_actor(self, session):
        session.safety.set_rate_limit("lh.tips_available", 1, 3600)
        op = Principal(id="agent:limited", role=Role.OPERATOR)
        proto = {"name": "rl", "steps": [
            {"name": f"s{i}", "device": "lh",
             "capability": "read:tips_available"} for i in range(3)]}
        out = dispatch(session, "run_protocol",
                       {"protocol": proto, "mode": "async"}, principal=op)
        ctx = GatewayContext.for_session(session)
        job = ctx.jobs.wait(out["result"]["job_id"], timeout=10)
        # Steps 2..3 must trip the per-actor rate limit under agent:limited.
        assert job.state.value == "failed"
        assert "Rate limit" in (job.error or "")

    def test_chunked_transfer_encoding_is_refused(self, server):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.putrequest("POST", "/tools/snapshot")
        conn.putheader("Authorization", "Bearer key-op")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"2\r\n{}\r\n0\r\n\r\n")
        resp = conn.getresponse()
        assert resp.status == 411
        conn.close()

    def test_post_without_content_length_is_refused(self, server):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.putrequest("POST", "/tools/snapshot", skip_accept_encoding=True)
        conn.putheader("Authorization", "Bearer key-op")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 411
        conn.close()

    def test_job_backpressure_cap(self, session, tmp_path):
        import threading

        from labaiagent.orchestration.jobs import JobManager
        gate = threading.Event()
        jm = JobManager(max_workers=1, max_active=2)

        class FakeSession:
            audit = session.audit
            def call(self, *a, **k):
                gate.wait(5)
                return "done"

        fake = FakeSession()
        jm.submit_call(fake, "d", "read:x")
        jm.submit_call(fake, "d", "read:x")
        from labaiagent.core.errors import LabAIAgentError
        with pytest.raises(LabAIAgentError, match="Too many jobs"):
            jm.submit_call(fake, "d", "read:x")
        gate.set()
        jm.shutdown()

    def test_sse_subscriber_cap(self):
        from labaiagent.gateway.events import EventBus
        bus = EventBus(max_subscribers=1)
        bus.subscribe()
        with pytest.raises(RuntimeError, match="Subscriber limit"):
            bus.subscribe()

    def test_filebacked_audit_keeps_no_memory_copy(self, session):
        session.write("lh", "flow_rate", value=33.0)
        assert session.audit._memory == []          # file IS the store
        assert any(r.event == "result" for r in session.audit.records())

    def test_emergency_stop_available_in_readonly_mode(self, session):
        names = set(tool_index(readonly_only=True))
        assert "emergency_stop" in names
        out = dispatch(session, "emergency_stop", {"reason": "t"},
                       readonly=True)
        assert out["ok"] and out["result"]["stopped"]
        ro_schema = {t["name"] for t in schemas.to_anthropic_tools(readonly_only=True)}
        assert "emergency_stop" in ro_schema

    def test_health_is_unauthenticated_but_minimal(self, server):
        req = urllib.request.Request(server.url + "/health")
        with urllib.request.urlopen(req) as r:
            out = json.loads(r.read())
        assert out == {"ok": True}                  # pulse only, no lab detail

    def test_internal_errors_are_opaque(self, session, monkeypatch):
        from labaiagent.gateway import registry as reg
        spec = reg.tool_index()["snapshot"]

        def boom(ctx, **kw):
            raise RuntimeError("/secret/path/config.yaml exploded")

        monkeypatch.setattr(spec, "handler", boom)
        out = dispatch(session, "snapshot", {})
        assert out["ok"] is False and out["error"] == "internal_error"
        assert "secret" not in json.dumps(out)
        assert out["error_id"]


# ==========================================================================
# Round-2 security layer (v1.1.0): keyed audit, TLS, throttling, caps
# ==========================================================================

class TestSecurityLayer:
    def test_keyed_audit_defeats_full_file_rewrite(self, tmp_path):
        """An attacker with write access can recompute an UNKEYED chain; the
        keyed chain must catch exactly that attack."""
        path = tmp_path / "keyed.jsonl"
        log = AuditLog(path, hmac_key="labkey-secret")
        log.record("invoke", device="d", arguments={"value": 10})
        log.record("result", device="d")
        assert log.verify()[0]

        # Full-file rewrite: attacker edits a value and recomputes every
        # link with plain SHA-256 (no key), preserving chain consistency.
        import hashlib as _h
        recs = [json.loads(x) for x in path.read_text().splitlines()]
        recs[0]["arguments"] = {"value": 99999}
        prev = "0" * 64
        for rec in recs:
            rec["prev_hash"] = prev
            body = {k: v for k, v in rec.items() if k != "hash"}
            rec["hash"] = _h.sha256(json.dumps(
                body, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False).encode()).hexdigest()
            prev = rec["hash"]
        path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")

        # Unkeyed verification is fooled -- this is the documented weakness.
        unkeyed = AuditLog(path)          # no key -> sha256 verification
        assert unkeyed.verify()[0] is True
        # Keyed verification catches it.
        keyed = AuditLog(path, hmac_key="labkey-secret")
        ok, msg = keyed.verify()
        assert ok is False

    def test_keyed_log_requires_matching_key(self, tmp_path):
        path = tmp_path / "k.jsonl"
        AuditLog(path, hmac_key="right").record("invoke", device="d")
        assert AuditLog(path, hmac_key="right").verify()[0]
        assert AuditLog(path, hmac_key="wrong").verify()[0] is False

    def test_auth_bruteforce_lockout(self, session, auth):
        from labaiagent.gateway.rest import AuthThrottle, GatewayServer
        throttle = AuthThrottle(max_failures=3, window_s=60, cooldown_s=60)
        srv = GatewayServer(session, host="127.0.0.1", port=0, auth=auth,
                            throttle=throttle).start()
        try:
            bad = LabClient(srv.url, api_key="guess")
            for _ in range(3):
                out = bad.call("list_devices")
                assert out["error"] == "unauthorized"
            out = bad.call("list_devices")
            assert out["error"] == "too_many_auth_failures"
            # Even the RIGHT key is refused during the cooldown (no oracle).
            good = LabClient(srv.url, api_key="key-op")
            assert good.call("list_devices")["error"] == "too_many_auth_failures"
        finally:
            srv.stop()

    def test_security_headers_present(self, server):
        req = urllib.request.Request(server.url + "/health")
        with urllib.request.urlopen(req) as r:
            assert r.headers["X-Content-Type-Options"] == "nosniff"
            assert r.headers["Cache-Control"] == "no-store"

    def test_tls_end_to_end(self, session, auth, tmp_path):
        import shutil
        import subprocess
        if not shutil.which("openssl"):
            pytest.skip("openssl unavailable")
        cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(key), "-out", str(cert), "-days", "1",
             "-subj", "/CN=127.0.0.1",
             "-addext", "subjectAltName=IP:127.0.0.1"],
            check=True, capture_output=True)
        from labaiagent.gateway.rest import GatewayServer
        srv = GatewayServer(session, host="127.0.0.1", port=0, auth=auth,
                            tls_cert=str(cert), tls_key=str(key)).start()
        try:
            assert srv.url.startswith("https://")
            lab = LabClient(srv.url, api_key="key-op", ca_file=str(cert))
            assert lab.health()["ok"]
            assert lab.read("lh", "flow_rate") == 100.0
        finally:
            srv.stop()

    def test_tls_requires_both_cert_and_key(self, session, auth):
        from labaiagent.core.errors import ConfigurationError
        from labaiagent.gateway.rest import GatewayServer
        with pytest.raises(ConfigurationError, match="BOTH"):
            GatewayServer(session, host="127.0.0.1", port=0, auth=auth,
                          tls_cert="only-cert.pem")

    def test_protocol_step_count_is_capped(self, session):
        proto = {"name": "huge", "steps": [
            {"name": f"s{i}", "device": "lh",
             "capability": "read:tips_available"} for i in range(501)]}
        out = dispatch(session, "run_protocol", {"protocol": proto})
        assert out["ok"] and out["result"]["valid"] is False
        assert "limit" in out["result"]["problems"][0]


# ==========================================================================
# Coverage of the stdio MCP loop, event frames, and the scaffold templates
# ==========================================================================

class TestStdioAndTemplates:
    def test_serve_stdio_full_round_trip(self, session):
        import io

        from labaiagent.mcp.server import serve_stdio
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "read_state",
                        "arguments": {"device_id": "lh",
                                      "capability": "flow_rate"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read",
             "params": {"uri": "lab://manifest"}},
            {"jsonrpc": "2.0", "id": 5, "method": "ping"},
            {"jsonrpc": "2.0", "id": 6, "method": "no/such/method"},
            "not json at all",
        ]
        fin = io.StringIO("\n".join(
            r if isinstance(r, str) else json.dumps(r) for r in requests))
        fout = io.StringIO()
        serve_stdio(session, stream_in=fin, stream_out=fout)
        replies = [json.loads(x) for x in fout.getvalue().splitlines()]
        by_id = {r.get("id"): r for r in replies}
        assert by_id[1]["result"]["serverInfo"]["name"] == "labaiagent"
        names = {t["name"] for t in by_id[2]["result"]["tools"]}
        assert "run_protocol" in names
        call_payload = json.loads(by_id[3]["result"]["content"][0]["text"])
        assert call_payload["ok"] and call_payload["result"]["value"] == 100.0
        assert "lh" in by_id[4]["result"]["contents"][0]["text"]
        assert by_id[5]["result"] == {}
        assert "error" in by_id[6]
        assert any(r.get("error", {}).get("code") == -32700 for r in replies)

    def test_readonly_stdio_hides_writes_keeps_estop(self, session):
        import io

        from labaiagent.mcp.server import serve_stdio
        fin = io.StringIO(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        fout = io.StringIO()
        serve_stdio(session, stream_in=fin, stream_out=fout, readonly=True)
        names = {t["name"] for t in
                 json.loads(fout.getvalue())["result"]["tools"]}
        assert "write_state" not in names and "emergency_stop" in names

    def test_event_bus_frames_and_unsubscribe(self):
        from labaiagent.gateway.events import EventBus
        bus = EventBus()
        stream = bus.sse_stream(heartbeat_s=0.05)
        assert next(stream) == b": connected\n\n"
        bus.publish("device.state", device="lh", to_state="busy")
        frame = next(stream)
        assert b"event: device.state" in frame and b"busy" in frame
        assert next(stream) == b": heartbeat\n\n"       # quiet period
        stream.close()
        assert bus._subs == []                          # unsubscribed

    def test_scaffolded_driver_source_is_valid_python(self, tmp_path):
        from labaiagent.templates.driver import (
            render_driver_template,
            render_test_template,
        )
        for transport in ("serial", "tcp", "http", "filewatch", "com",
                          "sila2", "subprocess", "sdk"):
            src = render_driver_template(
                key="acme.widget", class_name="AcmeWidget", vendor="Acme",
                model="WIDGET", category="pump", transport=transport)
            compile(src, f"acme_widget_{transport}.py", "exec")
        compile(render_test_template(key="acme.widget",
                                     class_name="AcmeWidget",
                                     module="acme_widget"),
                "test_acme_widget.py", "exec")
