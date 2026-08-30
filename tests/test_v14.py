"""v1.4.0: live hardware commands, provenance, parallel DAG, watchdog,
dashboard and metrics.

Hardware drivers are exercised against fake transports that speak the real
wire contracts (the Opentrons fake implements the actual robot-server
enqueue-then-poll shape verified against the published client). No test here
touches physical hardware; docs/VALIDATION_PLAN.md is the on-instrument
counterpart.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from labaiagent import LabSession
from labaiagent.core.capability import procedure, read
from labaiagent.core.device import Device
from labaiagent.core.errors import PhysicalError, SafetyViolation
from labaiagent.core.types import Param, Risk
from labaiagent.drivers.hardware import GenericSCPIInstrument, OpentronsFlex
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    World,
)
from labaiagent.gateway.registry import (
    TOOL_SPECS,
    GatewayContext,
    dispatch,
    tool_index,
)
from labaiagent.orchestration.watchdog import Watchdog
from labaiagent.orchestration.workflow import Protocol
from labaiagent.provenance.records import RunRecordStore, build_run_record

# ==========================================================================
# Fakes
# ==========================================================================

class FakeOTServer:
    """In-memory robot-server speaking the real HTTP API shapes:
    POST /runs, POST /runs/{id}/commands (enqueue), GET .../commands/{id}
    (poll), /health, /runs (current run listing)."""

    def __init__(self) -> None:
        self.commands: list[dict[str, Any]] = []   # every enqueued command
        self.current_runs: list[dict[str, Any]] = []
        self.fail_next: dict[str, Any] | None = None
        self.runs_created: list[str] = []
        self.stop_actions: list[str] = []
        self.transient_polls = 0        # raise TransientError on this many GETs
        self.hold_running = False       # command polls never settle
        self._n = 0

    def open(self) -> None: ...
    def close(self) -> None: ...

    def request(self, payload: dict[str, Any], **kw: Any) -> Any:
        method, path = payload.get("method", "GET"), payload["path"]
        body = payload.get("json")
        if path == "/health":
            return {"api_version": "7.1", "fw_version": "fake"}
        if method == "GET" and path == "/runs":
            return {"data": self.current_runs}
        if method == "POST" and path == "/runs":
            self._n += 1
            rid = f"run{self._n}"
            self.runs_created.append(rid)
            return {"data": {"id": rid}}
        if method == "POST" and path.endswith("/actions"):
            self.stop_actions.append(path.split("/")[2])
            return {}
        if method == "POST" and path.endswith("/commands"):
            self._n += 1
            cid = f"cmd{self._n}"
            entry = {"id": cid, **(body or {}).get("data", {})}
            if self.fail_next is not None:
                entry["_fail"] = self.fail_next
                self.fail_next = None
            self.commands.append(entry)
            return {"data": {"id": cid}}
        if method == "GET" and "/commands/" in path:
            if self.transient_polls > 0:
                self.transient_polls -= 1
                from labaiagent.core.errors import TransientError
                raise TransientError("fake 503 from robot-server")
            cid = path.rsplit("/", 1)[1]
            entry = next(c for c in self.commands if c["id"] == cid)
            if self.hold_running:
                return {"data": {"status": "running"}}
            if "_fail" in entry:
                return {"data": {"status": "failed", "error": entry["_fail"]}}
            result = {}
            if entry.get("commandType") == "loadPipette":
                result = {"pipetteId": "pip-1"}
            elif entry.get("commandType") == "loadLabware":
                result = {"labwareId": "lw-1"}
            elif entry.get("commandType") in ("aspirate", "dispense"):
                result = {"volume": entry["params"]["volume"]}
            return {"data": {"status": "succeeded", "result": result}}
        if method == "POST" and path == "/robot/home":
            return {}
        raise AssertionError(f"fake OT server: unhandled {method} {path}")


def make_flex(server: FakeOTServer) -> OpentronsFlex:
    dev = OpentronsFlex("flex", config={"host": "10.0.0.5"})
    dev._http = server  # type: ignore[assignment]
    return dev


class FakeSCPILink:
    def __init__(self, replies: dict[str, list[str]]) -> None:
        self.replies = {k: list(v) for k, v in replies.items()}
        self.sent: list[str] = []

    def request(self, cmd: str, **kw: Any) -> str:
        self.sent.append(cmd)
        q = self.replies.get(cmd)
        if q:
            return q.pop(0)
        return ""

    def open(self) -> None: ...
    def close(self) -> None: ...


class SlowBox(Device):
    """Test instrument whose one procedure sleeps and records its interval."""
    vendor, model, category = "test", "SlowBox", "generic"

    def __init__(self, device_id: str, *, delay: float = 0.3, **kw: Any) -> None:
        self.delay = delay
        self.intervals: list[tuple[float, float]] = []
        super().__init__(device_id, **kw)

    def _connect(self) -> None: ...
    def _disconnect(self) -> None: ...

    @read("ready", description="always ready")
    def get_ready(self) -> bool:
        return True

    @procedure("work", risk=Risk.LOW, description="sleep and return",
               params=[Param("label", str, default="x")])
    def do_work(self, label: str = "x") -> str:
        t0 = time.monotonic()
        time.sleep(self.delay)
        self.intervals.append((t0, time.monotonic()))
        return label


class FlakyDev(Device):
    vendor, model, category = "test", "Flaky", "generic"

    def __init__(self, device_id: str, **kw: Any) -> None:
        self.healthy = True
        super().__init__(device_id, **kw)

    def _connect(self) -> None: ...
    def _disconnect(self) -> None: ...

    def _self_test(self) -> bool:
        return self.healthy

    @read("ok", description="liveness")
    def get_ok(self) -> bool:
        return self.healthy


# ==========================================================================
# Opentrons live commands
# ==========================================================================

class TestOpentronsLiveCommands:
    def test_load_and_pipette_cycle_hits_real_wire_shapes(self):
        srv = FakeOTServer()
        dev = make_flex(srv)
        dev.connect()

        pip = dev.invoke("proc:load_pipette",
                         pipette_name="p1000_single_flex", mount="left")
        assert pip["pipette_id"] == "pip-1"
        lw = dev.invoke("proc:load_labware",
                        load_name="corning_96_wellplate_360ul_flat", slot="1")
        assert lw["labware_id"] == "lw-1"
        out = dev.invoke("proc:aspirate", labware_id="lw-1", well="A1",
                         volume=50.0, pipette_id="pip-1")
        assert out["aspirated_uL"] == 50.0
        out = dev.invoke("proc:dispense", labware_id="lw-1", well="B1",
                         volume=50.0, pipette_id="pip-1")
        assert out["dispensed_uL"] == 50.0

        kinds = [c["commandType"] for c in srv.commands]
        assert kinds == ["loadPipette", "loadLabware", "aspirate", "dispense"]
        # Every live command is enqueued with intent=setup into ONE live run,
        # and aspirate carries the exact params the robot expects.
        assert all(c["intent"] == "setup" for c in srv.commands)
        asp = srv.commands[2]["params"]
        assert asp["wellLocation"]["origin"] == "bottom"
        assert asp["flowRate"] == 150.0 and asp["pipetteId"] == "pip-1"

    def test_failed_robot_command_is_a_physical_error_with_detail(self):
        srv = FakeOTServer()
        dev = make_flex(srv)
        dev.connect()
        dev.invoke("proc:load_pipette", pipette_name="p50_single_flex",
                   mount="right")
        srv.fail_next = {"errorType": "TipNotAttached",
                         "detail": "cannot aspirate without a tip"}
        with pytest.raises(PhysicalError, match="TipNotAttached"):
            dev.invoke("proc:aspirate", labware_id="lw-1", well="A1",
                       volume=10.0, pipette_id="pip-1")

    def test_refuses_live_session_beside_active_run(self):
        srv = FakeOTServer()
        srv.current_runs = [{"id": "runX", "current": True,
                             "status": "running"}]
        dev = make_flex(srv)
        dev.connect()
        with pytest.raises(PhysicalError, match="active run"):
            dev.invoke("proc:load_pipette", pipette_name="p50_single_flex",
                       mount="left")

    def test_safety_engine_gates_each_aspirate_before_transport(self):
        srv = FakeOTServer()
        dev = make_flex(srv)
        session = LabSession([dev])
        session.connect_all()
        with pytest.raises(SafetyViolation):
            session.run("flex", "aspirate", labware_id="lw-1", well="A1",
                        volume=5000.0, pipette_id="pip-1")   # > 1000 uL limit
        # The refusal happened BEFORE any command reached the robot.
        assert srv.commands == []
        refused = [r for r in session.audit.records() if r.event == "refused"]
        assert refused and refused[-1].capability == "aspirate"


# ==========================================================================
# SCPI hardening
# ==========================================================================

class TestSCPIHardening:
    def _dev(self, replies: dict[str, list[str]],
             **cfg: Any) -> tuple[GenericSCPIInstrument, FakeSCPILink]:
        dev = GenericSCPIInstrument(
            "scpi", config={"scheme": "tcp",
                            "commands": {"set_value": "SOUR:TEMP {value:.1f}"},
                            **cfg})
        link = FakeSCPILink(replies)
        dev._link = link  # type: ignore[assignment]
        return dev, link

    def test_error_queue_drains_until_zero(self):
        dev, _ = self._dev({"SYST:ERR?": [
            '-113,"Undefined header"', '-222,"Data out of range"',
            '+0,"No error"']})
        errors = dev.get_error_queue()
        assert len(errors) == 2 and "range" in errors[1]

    def test_rejected_setpoint_becomes_physical_error(self):
        # Pre-drain finds a clean queue; the error appears AFTER the write.
        dev, link = self._dev({"SYST:ERR?": [
            '0,"No error"', '-222,"Data out of range"', '0,"No error"']})
        with pytest.raises(PhysicalError, match="rejected the setpoint"):
            dev.set_value(999.0)
        assert "SOUR:TEMP 999.0" in link.sent

    def test_r4_9_stale_queue_errors_not_blamed_on_this_write(self):
        # Review finding R4-9: an unrelated error already sitting in the
        # queue must be discarded by the pre-drain, not attributed to this
        # setpoint.
        dev, link = self._dev({"SYST:ERR?": [
            '-350,"Queue overflow"', '0,"No error"',   # stale, pre-drain
            '0,"No error"']})                          # post-write: clean
        assert dev.set_value(37.0) == 37.0
        assert link.sent.count("SYST:ERR?") == 3

    def test_clean_write_passes_and_verification_can_be_disabled(self):
        dev, _ = self._dev({"SYST:ERR?": ['0,"No error"', '0,"No error"']})
        assert dev.set_value(37.0) == 37.0
        dev2, link2 = self._dev({}, verify_writes=False)
        assert dev2.set_value(25.0) == 25.0
        assert "SYST:ERR?" not in link2.sent


# ==========================================================================
# Parallel DAG execution
# ==========================================================================

class TestParallelProtocols:
    def _session(self, delay: float = 0.3) -> LabSession:
        a, b = SlowBox("boxa", delay=delay), SlowBox("boxb", delay=delay)
        s = LabSession([a, b], autonomy_ceiling=Risk.HIGH)
        s.connect_all()
        return s

    def _proto(self) -> Protocol:
        p = Protocol("par-test")
        p.step("s1", "boxa", "proc:work", args={"label": "a"}, store_as="ra")
        p.step("s2", "boxb", "proc:work", args={"label": "b"}, store_as="rb")
        p.step("s3", "boxa", "proc:work", args={"label": "$ra"},
               depends_on=("s1", "s2"))
        return p

    def test_independent_steps_actually_overlap(self):
        s = self._session()
        summary = self._proto().run(s, parallel=True)
        assert summary["counts"] == {"done": 3}
        (a0, a1) = s.get("boxa").intervals[0]
        (b0, b1) = s.get("boxb").intervals[0]
        assert a0 < b1 and b0 < a1, "wave steps did not overlap"
        # The dependent step ran only after both parents.
        (c0, _) = s.get("boxa").intervals[1]
        assert c0 >= max(a1, b1) - 0.01

    def test_context_and_results_correct_under_parallelism(self):
        s = self._session(delay=0.05)
        p = self._proto()
        p.run(s, parallel=True)
        assert p.context["ra"] == "a" and p.context["rb"] == "b"
        assert p.steps[2].result == "a"     # $ra resolved after the wave

    def test_same_device_steps_serialise_on_the_device_lock(self):
        a = SlowBox("boxa", delay=0.15)
        s = LabSession([a], autonomy_ceiling=Risk.HIGH)
        s.connect_all()
        p = Protocol("serial-test")
        p.step("s1", "boxa", "proc:work")
        p.step("s2", "boxa", "proc:work")
        p.run(s, parallel=True)
        (a0, a1), (b0, b1) = sorted(a.intervals)
        assert b0 >= a1 - 0.01, "two actuations interleaved on one device"

    def test_abort_in_wave_skips_downstream(self):
        s = self._session(delay=0.02)
        p = Protocol("abort-test")
        p.step("bad", "boxa", "proc:work", args={"label": 123})  # type error
        p.step("later", "boxb", "proc:work", depends_on=("bad",))
        from labaiagent.core.errors import WorkflowError
        with pytest.raises(WorkflowError):
            p.run(s, parallel=True, validate=False)
        assert p.steps[1].status.value == "skipped"


# ==========================================================================
# Run-record provenance
# ==========================================================================

def _world_session(tmp_path: Path) -> LabSession:
    world = World(seed=7)
    s = LabSession([SimulatedLiquidHandler("lh", world=world),
                    SimulatedPlateReader("reader", world=world)],
                   audit_path=tmp_path / "audit.jsonl")
    s.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(100.0, {"dye": 5.0})
    return s


PROTO = {"name": "prov", "steps": [
    {"name": "tips", "device": "lh", "capability": "read:tips_available"},
    {"name": "mix", "device": "lh", "capability": "proc:transfer",
     "args": {"source_barcode": "P1", "source_well": "A1",
              "dest_barcode": "P1", "dest_well": "B1", "volume": 20.0},
     "depends_on": ["tips"]}]}


class TestRunRecords:
    def test_protocol_run_writes_verifiable_record(self, tmp_path):
        s = _world_session(tmp_path)
        ctx = GatewayContext.for_session(s)
        ctx.records = RunRecordStore(tmp_path / "records", hmac_key=b"k1")
        out = dispatch(ctx, "run_protocol", {"protocol": PROTO})
        assert out["ok"] and out["result"]["record_id"]
        rid = out["result"]["record_id"]
        ok, msg = ctx.records.verify(rid)
        assert ok, msg
        body = ctx.records.get(rid)["body"]
        assert body["status"] == "done"
        assert body["software"]["labaiagent"]
        assert {d["id"] for d in body["devices"]} == {"lh"}
        assert body["devices"][0]["driver_version"]
        # The embedded audit slice covers the run, hashes intact.
        assert body["audit_slice"]["n_records"] >= 4
        assert all(r["hash"] for r in body["audit_slice"]["records"])
        # Protocol-as-executed carries per-step results.
        steps = {st["name"]: st for st in body["protocol"]["steps"]}
        assert steps["mix"]["status"] == "done"

    def test_tamper_is_detected(self, tmp_path):
        s = _world_session(tmp_path)
        ctx = GatewayContext.for_session(s)
        ctx.records = RunRecordStore(tmp_path / "records", hmac_key=b"k1")
        rid = dispatch(ctx, "run_protocol",
                       {"protocol": PROTO})["result"]["record_id"]
        path = ctx.records._path(rid)
        sealed = json.loads(path.read_text())
        sealed["body"]["actor"] = "user:mallory"
        path.write_text(json.dumps(sealed))
        ok, msg = ctx.records.verify(rid)
        assert not ok and "mismatch" in msg
        # And a wrong key also fails, even on an untampered record.
        rid2 = dispatch(ctx, "run_protocol",
                        {"protocol": PROTO})["result"]["record_id"]
        other = RunRecordStore(tmp_path / "records", hmac_key=b"WRONG")
        ok2, _ = other.verify(rid2)
        assert not ok2

    def test_run_records_tool_surface(self, tmp_path):
        s = _world_session(tmp_path)
        ctx = GatewayContext.for_session(s)
        ctx.records = RunRecordStore(tmp_path / "records")
        rid = dispatch(ctx, "run_protocol",
                       {"protocol": PROTO})["result"]["record_id"]
        listed = dispatch(ctx, "run_records", {"action": "list"})
        assert listed["ok"]
        assert any(r["record_id"] == rid for r in listed["result"]["records"])
        got = dispatch(ctx, "run_records", {"action": "get", "record_id": rid})
        assert got["result"]["record"]["body"]["protocol"]["name"] == "prov"
        ver = dispatch(ctx, "run_records",
                       {"action": "verify", "record_id": rid})
        assert ver["result"]["valid"]

    def test_failed_run_still_gets_a_record(self, tmp_path):
        s = _world_session(tmp_path)
        ctx = GatewayContext.for_session(s)
        ctx.records = RunRecordStore(tmp_path / "records")
        bad = {"name": "prov-bad", "steps": [
            {"name": "boom", "device": "lh", "capability": "proc:transfer",
             "args": {"source_barcode": "P1", "source_well": "H12",
                      "dest_barcode": "P1", "dest_well": "A2",
                      "volume": 50.0}}]}
        out = dispatch(ctx, "run_protocol", {"protocol": bad})
        assert not out["ok"]
        recs = ctx.records.list()
        assert recs and recs[0]["status"] == "failed"

    def test_build_run_record_slices_only_this_protocol(self, tmp_path):
        s = _world_session(tmp_path)
        seq0 = s.audit.seq
        proto = Protocol.from_dict(PROTO)
        s.read("lh", "tips_available")          # unrelated traffic before
        proto.run(s)
        body = build_run_record(s, proto, status="done", seq_before=seq0)
        reasons = {r["reason"] for r in body["audit_slice"]["records"]}
        assert all(x.startswith("protocol:prov/") or x == "prov"
                   for x in reasons)


# ==========================================================================
# Watchdog
# ==========================================================================

class TestWatchdog:
    def test_trip_and_recover_cycle(self, tmp_path):
        dev = FlakyDev("flaky")
        s = LabSession([dev], audit_path=tmp_path / "a.jsonl")
        s.connect_all()
        wd = Watchdog(s, interval_s=100, failures_to_trip=3)
        assert wd.check_once()["flaky"] == "ok"
        dev.healthy = False
        wd.check_once()
        wd.check_once()
        assert dev.state.value == "idle"        # not yet: 2 of 3
        assert wd.check_once()["flaky"] == "tripped"
        assert dev.state.value == "error"
        assert "flaky" in wd.tripped
        events = [r.event for r in s.audit.records()]
        assert "heartbeat_lost" in events
        # The state gate now refuses actuation-by-read? No: reads are fine,
        # but the device reports ERROR to anything that checks state.
        dev.healthy = True
        assert wd.check_once()["flaky"] == "recovered"
        assert dev.state.value == "idle"
        assert "heartbeat_recovered" in [r.event for r in s.audit.records()]

    def test_never_probes_busy_or_estop(self, tmp_path):
        dev = FlakyDev("flaky")
        s = LabSession([dev], audit_path=tmp_path / "a.jsonl")
        s.connect_all()
        wd = Watchdog(s, interval_s=100, failures_to_trip=1)
        from labaiagent.core.types import DeviceState
        dev._set_state(DeviceState.BUSY)
        assert "skipped" in wd.check_once()["flaky"]
        dev._set_state(DeviceState.IDLE)
        s.emergency_stop("test")
        assert wd.check_once() == {}


# ==========================================================================
# Dashboard, metrics, tool surface
# ==========================================================================

class TestOpsSurface:
    def _server(self, tmp_path):
        from labaiagent.gateway.rest import GatewayServer
        s = _world_session(tmp_path)
        srv = GatewayServer(s, host="127.0.0.1", port=0).start()
        return s, srv

    def test_dashboard_served_at_root_with_csp(self, tmp_path):
        s, srv = self._server(tmp_path)
        try:
            with urllib.request.urlopen(f"{srv.url}/") as r:
                html = r.read().decode()
                assert "LabAIAgent" in html and "E-STOP" in html
                assert "connect-src 'self'" in r.headers.get(
                    "Content-Security-Policy", "")
                assert r.headers["X-Content-Type-Options"] == "nosniff"
        finally:
            srv.stop()
            s.disconnect_all()

    def test_metrics_prometheus_format(self, tmp_path):
        s, srv = self._server(tmp_path)
        try:
            with urllib.request.urlopen(f"{srv.url}/metrics") as r:
                text = r.read().decode()
            assert "labaiagent_up 1" in text
            assert 'labaiagent_devices{state="idle"} 2' in text
            assert "# TYPE labaiagent_emergency_stop_active gauge" in text
            assert "labaiagent_tasks" in text          # memory wired
            assert "labaiagent_run_records" in text    # records wired
        finally:
            srv.stop()
            s.disconnect_all()

    def test_tool_surface_is_twenty_and_run_records_readonly(self):
        assert len(TOOL_SPECS) == 20
        idx = tool_index(readonly_only=True)
        assert "run_records" in idx and idx["run_records"].readonly

    def test_watchdog_thread_lifecycle(self, tmp_path):
        dev = FlakyDev("flaky")
        s = LabSession([dev], audit_path=tmp_path / "a.jsonl")
        s.connect_all()
        wd = Watchdog(s, interval_s=0.05, failures_to_trip=2).start()
        dev.healthy = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and "flaky" not in wd.tripped:
            time.sleep(0.02)
        wd.stop()
        assert "flaky" in wd.tripped
        assert not any(t.name == "labaiagent-watchdog" and t.is_alive()
                       for t in threading.enumerate())


# ==========================================================================
# Round-4 review regressions (see REVIEW.md, round 4)
# ==========================================================================

class TestRound4Regressions:
    def test_r4_1_parallel_abort_stops_other_lanes(self):
        """A FAILED+ABORT step must stop every lane dispatching further
        steps -- physical actuation must not continue after an abort."""
        a = SlowBox("boxa", delay=0.01)
        b = SlowBox("boxb", delay=0.25)
        s = LabSession([a, b], autonomy_ceiling=Risk.HIGH)
        s.connect_all()
        p = Protocol("r4-1")
        p.step("bad", "boxa", "proc:work", args={"label": 123})   # refused fast
        p.step("b1", "boxb", "proc:work")
        p.step("b2", "boxb", "proc:work")
        p.step("b3", "boxb", "proc:work")
        from labaiagent.core.errors import WorkflowError
        with pytest.raises(WorkflowError):
            p.run(s, parallel=True, validate=False)
        by = {st.name: st.status.value for st in p.steps}
        assert by["bad"] == "failed"
        # b1 may have been mid-flight when the abort landed; b2/b3 must NOT
        # have actuated.
        assert by["b3"] == "skipped" and by["b2"] == "skipped"
        assert len(b.intervals) <= 1

    def test_r4_1_parallel_cancellation_honoured_within_wave(self):
        a = SlowBox("boxa", delay=0.15)
        b = SlowBox("boxb", delay=0.15)
        s = LabSession([a, b], autonomy_ceiling=Risk.HIGH)
        s.connect_all()
        p = Protocol("r4-1c")
        for i in range(3):
            p.step(f"a{i}", "boxa", "proc:work")
            p.step(f"b{i}", "boxb", "proc:work")
        fired = {"n": 0}

        def cancel_after_first() -> bool:
            fired["n"] += 1
            return len(a.intervals) + len(b.intervals) >= 2

        summary = p.run(s, parallel=True, should_cancel=cancel_after_first)
        assert summary["cancelled"] is True
        # Six steps were queued; cancellation after ~2 must skip most of the
        # rest instead of running all six.
        assert summary["counts"].get("skipped", 0) >= 2
        assert len(a.intervals) + len(b.intervals) <= 4

    def test_r4_2_poll_blip_does_not_double_actuate(self):
        """A transient error while POLLING an enqueued command must never
        escape to the session retry layer -- that re-enqueues the aspirate."""
        srv = FakeOTServer()
        dev = make_flex(srv)
        session = LabSession([dev], autonomy_ceiling=Risk.HIGH)
        session.connect_all()
        srv.transient_polls = 2          # two 503s, then a clean answer
        out = session.run("flex", "aspirate", labware_id="lw-1", well="A1",
                          volume=25.0, pipette_id="pip-1")
        assert out["aspirated_uL"] == 25.0
        aspirates = [c for c in srv.commands if c["commandType"] == "aspirate"]
        assert len(aspirates) == 1, "retry layer double-enqueued the command"

    def test_r4_2_poll_timeout_is_physical_never_transient(self):
        srv = FakeOTServer()
        dev = make_flex(srv)
        dev.connect()
        srv.hold_running = True
        with pytest.raises(PhysicalError, match="may still execute"):
            dev._command("aspirate", {"volume": 1.0}, timeout=0.3, poll_s=0.05)

    def test_r4_3_records_with_non_json_values_still_verify(self, tmp_path):
        from datetime import datetime, timezone
        store = RunRecordStore(tmp_path / "rec", hmac_key=b"k")
        body = {"record_id": "abc123", "created_at": "t", "status": "done",
                "exotic": {"when": datetime.now(timezone.utc),
                           "where": Path("/tmp/x"),
                           "vals": (1, 2, 3)}}
        rid = store.save(body)
        ok, msg = store.verify(rid)
        assert ok, msg

    def test_r4_4_probe_skipped_while_device_lock_held(self, tmp_path):
        dev = FlakyDev("flaky")
        s = LabSession([dev], audit_path=tmp_path / "a.jsonl")
        s.connect_all()
        wd = Watchdog(s, interval_s=100, failures_to_trip=1)
        lock = s._locks["flaky"]
        held = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with lock:
                held.set()
                release.wait(timeout=5)

        t = threading.Thread(target=holder)
        t.start()
        held.wait(timeout=5)
        try:
            dev.healthy = False
            out = wd.check_once()
            assert "lock busy" in out["flaky"]
            assert dev.state.value == "idle"     # never tripped mid-actuation
        finally:
            release.set()
            t.join()

    def test_r4_5_recovery_never_clears_error_it_did_not_set(self, tmp_path):
        dev = FlakyDev("flaky")
        s = LabSession([dev], audit_path=tmp_path / "a.jsonl")
        s.connect_all()
        wd = Watchdog(s, interval_s=100, failures_to_trip=1)
        dev.healthy = False
        assert wd.check_once()["flaky"] == "tripped"
        # A physical fault re-asserts ERROR with its own message.
        from labaiagent.core.types import DeviceState
        dev._set_state(DeviceState.ERROR, error="PhysicalError: crash into deck")
        dev.healthy = True
        out = wd.check_once()["flaky"]
        assert "left in current state" in out
        assert dev.state.value == "error", "watchdog cleared a fault it didn't own"

    def test_r4_5_recovery_never_lifts_a_quarantine(self, tmp_path):
        class FakeSup:
            def is_quarantined(self, device_id: str) -> bool:
                return True

        dev = FlakyDev("flaky")
        s = LabSession([dev], audit_path=tmp_path / "a.jsonl")
        s.connect_all()
        wd = Watchdog(s, interval_s=100, failures_to_trip=1,
                      supervisor=FakeSup())
        dev.healthy = False
        wd.check_once()
        dev.healthy = True
        out = wd.check_once()["flaky"]
        assert "quarantined" in out and dev.state.value == "error"

    def test_r4_6_cancel_before_start_still_settles(self, tmp_path):
        from labaiagent.orchestration.jobs import JobManager
        a = SlowBox("boxa", delay=0.4)
        s = LabSession([a], audit_path=tmp_path / "a.jsonl",
                       autonomy_ceiling=Risk.HIGH)
        s.connect_all()
        jobs = JobManager(max_workers=1)
        blocker = jobs.submit_call(s, "boxa", "proc:work", {})
        p = Protocol("r4-6")
        p.step("w", "boxa", "proc:work")
        settled: list[str] = []
        queued = jobs.submit_protocol(s, p, on_done=lambda j: settled.append(
            j.state.value))
        jobs.cancel(queued.id)              # cancelled while still queued
        jobs.wait(blocker.id, timeout=10)
        jobs.wait(queued.id, timeout=10)
        deadline = time.monotonic() + 5.0   # on_done fires just after the
        while not settled and time.monotonic() < deadline:   # terminal state
            time.sleep(0.01)
        assert settled == ["cancelled"], "on_done skipped for early cancel"
        finished = [r for r in s.audit.records() if r.event == "job_finished"
                    and r.result and r.result.get("job_id") == queued.id]
        assert finished and finished[0].result["state"] == "cancelled"
        jobs.shutdown()

    def test_r4_7_resume_restores_context_for_dollar_refs(self, tmp_path):
        a = SlowBox("boxa", delay=0.01)
        s = LabSession([a], autonomy_ceiling=Risk.HIGH)
        s.connect_all()
        cp = tmp_path / "cp.json"
        # First run: only step s1 exists; it stores its result. This is the
        # state a crash leaves behind.
        p1 = Protocol("r4-7", checkpoint_path=cp)
        p1.step("s1", "boxa", "proc:work", args={"label": "seed"},
                store_as="ra")
        p1.run(s)
        # Restart: the full protocol document (s1 + dependent s2) is rebuilt
        # from scratch and resumed.
        p2 = Protocol("r4-7", checkpoint_path=cp)
        p2.step("s1", "boxa", "proc:work", args={"label": "seed"},
                store_as="ra")
        p2.step("s2", "boxa", "proc:work", args={"label": "$ra"},
                depends_on=("s1",))
        assert p2.resume_from_checkpoint() == 1
        assert p2.context["ra"] == "seed"
        summary = p2.run(s, validate=False)
        assert summary["counts"]["done"] >= 1
        assert p2.steps[1].result == "seed"     # $ra resolved post-crash
        assert len(a.intervals) == 2            # s1 was NOT re-run

    def test_r4_8_halt_invalidates_live_run(self):
        srv = FakeOTServer()
        dev = make_flex(srv)
        dev.connect()
        dev.invoke("proc:load_pipette", pipette_name="p50_single_flex",
                   mount="left")
        assert len(srv.runs_created) == 1
        dev._halt()
        assert srv.stop_actions, "e-stop did not stop the live run"
        # Next command must open a FRESH run, not post into the dead one.
        dev.invoke("proc:load_pipette", pipette_name="p50_single_flex",
                   mount="right")
        assert len(srv.runs_created) == 2

    def test_r4_10_dashboard_escapes_attribute_context(self):
        from labaiagent.gateway.dashboard import DASHBOARD_HTML
        assert '&quot;' in DASHBOARD_HTML and "&#39;" in DASHBOARD_HTML
        assert "/^[a-z_]+$/" in DASHBOARD_HTML   # state-class whitelist

    def test_r4_12_server_stop_can_shut_down_job_pool(self, tmp_path):
        from labaiagent.gateway.rest import GatewayServer
        s = _world_session(tmp_path)
        srv = GatewayServer(s, host="127.0.0.1", port=0).start()
        srv.stop(shutdown_jobs=True)
        s.disconnect_all()
