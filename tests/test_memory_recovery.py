"""Tests for durable lab memory (restart continuity), the task board,
crash-resumable protocols, and incident intelligence (quarantine +
diagnosis).

The restart tests literally build a second LabMemory / Supervisor /
GatewayContext over the same files and assert the lab still knows where it
stopped and where it was going.

Run:  python -m pytest tests/test_memory_recovery.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from labaiagent import LabSession
from labaiagent.core.errors import LabAIAgentError
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    World,
)
from labaiagent.gateway.auth import Principal, Role
from labaiagent.gateway.registry import GatewayContext, dispatch
from labaiagent.memory.store import LabMemory
from labaiagent.oversight.supervisor import Supervisor

OP = Principal(id="agent:op", role=Role.OPERATOR)
HUMAN = Principal(id="user:babu", kind="human", role=Role.APPROVER)


@pytest.fixture
def world():
    return World(seed=21)


@pytest.fixture
def session(world, tmp_path):
    s = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world),
         SimulatedRobotArm("arm", world=world)],
        name="mem-test", audit_path=tmp_path / "audit.jsonl",
        actor="user:test")
    s.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    world.add_labware(Labware("DIL", n_wells=6, kind="reservoir",
                              location="deck_2"))
    world.get("DIL").well("A1").add(3000.0, {})
    yield s
    s.disconnect_all()


def wire(session, tmp_path) -> GatewayContext:
    ctx = GatewayContext.for_session(session)
    ctx.memory = LabMemory(tmp_path / "state.db")
    ctx.supervisor = Supervisor(max_refusals=3, window_s=60,
                                memory=ctx.memory)
    return ctx


# ==========================================================================
# Durable memory primitives
# ==========================================================================

class TestLabMemory:
    def test_kv_survives_reopen(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        m.remember("run", "last_plate", {"barcode": "P1", "well": "B3"})
        m.close()
        m2 = LabMemory(tmp_path / "s.db")
        assert m2.recall("run", "last_plate")["well"] == "B3"
        assert m2.recall("run", "missing", "fallback") == "fallback"

    def test_tasks_survive_reopen_and_order_by_priority(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        m.add_task("low", "later", created_by="user:babu", priority=9)
        urgent = m.add_task("urgent", "now", created_by="user:babu",
                            priority=1)
        m.close()
        m2 = LabMemory(tmp_path / "s.db")
        assert m2.next_task()["id"] == urgent["id"]
        assert len(m2.list_tasks("pending")) == 2

    def test_interrupted_work_resumes_first(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        m.add_task("new work", "x", created_by="u", priority=1)
        old = m.add_task("was running", "x", created_by="u", priority=9)
        m.update_task(old["id"], status="in_progress")
        # Simulated crash: fresh handle. Interrupted work outranks even a
        # higher-priority pending task.
        m2 = LabMemory(tmp_path / "s.db")
        assert m2.next_task()["id"] == old["id"]
        rep = m2.startup_report()
        assert rep["interrupted_tasks"][0]["title"] == "was running"

    def test_incidents_lifecycle(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        inc = m.open_incident(device="lh", capability="proc:transfer",
                              error_type="PhysicalError", message="tip jam")
        assert m.list_incidents("open")[0]["id"] == inc["id"]
        with pytest.raises(LabAIAgentError):
            m.resolve_incident(inc["id"], resolution="cleared", by="")
        m.resolve_incident(inc["id"], resolution="cleared jam", by="user:babu")
        assert m.list_incidents("open") == []

    def test_checkpoint_paths_are_server_derived(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        evil = m.checkpoint_path("../../etc/passwd")
        assert evil.parent == m.checkpoint_dir           # traversal stripped
        assert ".." not in evil.name


# ==========================================================================
# Suspension and quarantine persist across restart
# ==========================================================================

class TestOversightPersistence:
    def test_suspension_survives_restart(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        sup = Supervisor(memory=m)
        sup.suspend("agent:evil", "argued with limits")
        # Crash + restart: new Supervisor over the same store.
        sup2 = Supervisor(memory=LabMemory(tmp_path / "s.db"))
        assert sup2.is_suspended("agent:evil")
        sup2.reinstate("agent:evil", operator="user:babu")
        sup3 = Supervisor(memory=LabMemory(tmp_path / "s.db"))
        assert not sup3.is_suspended("agent:evil")

    def test_quarantine_survives_restart(self, tmp_path):
        m = LabMemory(tmp_path / "s.db")
        sup = Supervisor(memory=m)
        sup.quarantine("lh", "aperture clog")
        sup2 = Supervisor(memory=LabMemory(tmp_path / "s.db"))
        assert sup2.is_quarantined("lh")
        assert "clog" in sup2.quarantine_reason("lh")


# ==========================================================================
# Incident intelligence: acting on a physical failure
# ==========================================================================

class TestIncidentIntelligence:
    def test_physical_failure_quarantines_and_diagnoses(self, session,
                                                        tmp_path):
        ctx = wire(session, tmp_path)
        # Aspirating more than the well holds -> PhysicalError from the lh.
        out = dispatch(ctx, "run_procedure",
                       {"device_id": "lh", "capability": "transfer",
                        "arguments": {"source_barcode": "P1",
                                      "source_well": "H12",   # empty well
                                      "dest_barcode": "P1", "dest_well": "A2",
                                      "volume": 100.0},
                        "reason": "test transfer"}, principal=OP)
        assert out["ok"] is False and out["error"] == "PhysicalError"
        # The intelligent response arrived WITH the failure:
        assert out["incident"]["device"] == "lh"
        diag = out["diagnosis"]
        assert "Do NOT retry" in diag["recommended_actions"][0]
        assert diag["operating_notes"]        # driver notes surfaced
        # ...and the device is now quarantined:
        blocked = dispatch(ctx, "write_state",
                           {"device_id": "lh", "capability": "flow_rate",
                            "arguments": {"value": 50},
                            "reason": "should be blocked"}, principal=OP)
        assert blocked["error"] == "oversight_denied"
        assert blocked["reviewer"] == "quarantine"
        # Reads still work; other devices unaffected.
        assert dispatch(ctx, "read_state",
                        {"device_id": "lh", "capability": "flow_rate"},
                        principal=OP)["ok"]
        assert dispatch(ctx, "write_state",
                        {"device_id": "arm", "capability": "speed",
                         "arguments": {"value": 40},
                         "reason": "unaffected device"}, principal=OP)["ok"]

    def test_human_resolution_lifts_quarantine(self, session, tmp_path):
        ctx = wire(session, tmp_path)
        ctx.supervisor.quarantine("lh", "clog", session=session)
        inc = ctx.memory.open_incident(device="lh", capability="proc:transfer",
                                       error_type="PhysicalError",
                                       message="clog")
        # Agents may not resolve incidents.
        denied = dispatch(ctx, "lab_tasks",
                          {"action": "resolve_incident",
                           "incident_id": inc["id"],
                           "resolution": "I promise it is fine"},
                          principal=OP)
        assert denied["ok"] is False
        # A human does; the quarantine lifts.
        ok = dispatch(ctx, "lab_tasks",
                      {"action": "resolve_incident", "incident_id": inc["id"],
                       "resolution": "flushed aperture, verified"},
                      principal=HUMAN)
        assert ok["ok"] and ok["result"]["quarantine_released"]
        again = dispatch(ctx, "write_state",
                         {"device_id": "lh", "capability": "flow_rate",
                          "arguments": {"value": 60},
                          "reason": "post-fix check"}, principal=OP)
        assert again["ok"], again

    def test_protocols_refuse_quarantined_devices(self, session, tmp_path):
        ctx = wire(session, tmp_path)
        ctx.supervisor.quarantine("lh", "clog", session=session)
        out = dispatch(ctx, "run_protocol", {"protocol": {
            "name": "p", "steps": [
                {"name": "s", "device": "lh",
                 "capability": "read:tips_available"}]}}, principal=OP)
        assert out["ok"] is False and out["error"] == "oversight_denied"
        assert "QUARANTINED" in out["message"] or "quarantined" in out["message"]


# ==========================================================================
# The task board: do the tasks the user recommended, across restarts
# ==========================================================================

class TestTaskBoard:
    def test_only_humans_add_and_cancel(self, session, tmp_path):
        ctx = wire(session, tmp_path)
        denied = dispatch(ctx, "lab_tasks",
                          {"action": "add", "title": "agent self-task"},
                          principal=OP)
        assert denied["ok"] is False and "approver" in denied["message"]
        ok = dispatch(ctx, "lab_tasks",
                      {"action": "add", "title": "Run dilution series",
                       "instructions": "P1 wells A1-D1"}, principal=HUMAN)
        assert ok["ok"]
        tid = ok["result"]["task"]["id"]
        assert any(r.event == "task_added" for r in session.audit.records())
        cancelled = dispatch(ctx, "lab_tasks",
                             {"action": "cancel", "task_id": tid},
                             principal=HUMAN)
        assert cancelled["result"]["task"]["status"] == "cancelled"

    def test_task_with_protocol_runs_and_settles(self, session, tmp_path,
                                                 world):
        ctx = wire(session, tmp_path)
        proto = {"name": "dilute", "steps": [
            {"name": "dilute", "device": "lh",
             "capability": "proc:serial_dilution",
             "args": {"barcode": "P1", "wells": ["A1", "B1", "C1"],
                      "transfer_volume": 50.0, "diluent_barcode": "DIL",
                      "diluent_well": "A1", "diluent_volume": 50.0}}]}
        task = dispatch(ctx, "lab_tasks",
                        {"action": "add", "title": "dilution series",
                         "instructions": "per SOP", "protocol": proto},
                        principal=HUMAN)["result"]["task"]
        run = dispatch(ctx, "run_protocol", {"task_id": task["id"]},
                       principal=OP)
        assert run["ok"], run
        settled = ctx.memory.get_task(task["id"])
        assert settled["status"] == "done"
        assert settled["result"]["counts"]["done"] == 1
        assert world.get("P1").well("B1").volume_uL > 0

    def test_crash_resume_skips_completed_steps(self, session, tmp_path):
        """The continuity guarantee: after a 'crash', the same task resumes
        from its checkpoint instead of re-running finished steps."""
        ctx = wire(session, tmp_path)
        proto = {"name": "two_reads", "steps": [
            {"name": "one", "device": "lh", "capability": "read:tips_available"},
            {"name": "two", "device": "lh", "capability": "read:flow_rate"}]}
        task = dispatch(ctx, "lab_tasks",
                        {"action": "add", "title": "resumable",
                         "protocol": proto}, principal=HUMAN)["result"]["task"]
        # First run completes step one, then we simulate a crash by marking
        # the task interrupted with a checkpoint already on disk.
        run = dispatch(ctx, "run_protocol", {"task_id": task["id"]},
                       principal=OP)
        assert run["ok"]
        cp = ctx.memory.checkpoint_path(task["id"])
        assert cp.exists()
        ctx.memory.update_task(task["id"], status="in_progress",
                               note="simulated crash", by="test")

        # ---- restart: new memory handle over the same directory ----
        mem2 = LabMemory(tmp_path / "state.db")
        rep = mem2.startup_report()
        assert rep["interrupted_tasks"][0]["id"] == task["id"]
        assert rep["interrupted_tasks"][0]["has_checkpoint"]
        ctx.memory = mem2
        resumed = dispatch(ctx, "run_protocol",
                           {"task_id": task["id"], "resume": True},
                           principal=OP)
        assert resumed["ok"], resumed
        assert resumed["result"]["resumed_steps"] == 2   # nothing re-ran
        assert mem2.get_task(task["id"])["status"] == "done"

    def test_list_shows_continuity(self, session, tmp_path):
        ctx = wire(session, tmp_path)
        dispatch(ctx, "lab_tasks", {"action": "add", "title": "t1"},
                 principal=HUMAN)
        ctx.supervisor.quarantine("reader", "lamp fault", session=session)
        ctx.memory.open_incident(device="reader", capability="read",
                                 error_type="PhysicalError", message="lamp")
        out = dispatch(ctx, "lab_tasks", {"action": "list"}, principal=OP)
        r = out["result"]
        assert r["next_task"]["title"] == "t1"
        assert r["open_incidents"][0]["device"] == "reader"
        assert r["quarantined_devices"][0]["device"] == "reader"

    def test_failed_physical_task_keeps_checkpoint_for_after_repair(
            self, session, tmp_path):
        ctx = wire(session, tmp_path)
        proto = {"name": "bad", "steps": [
            {"name": "ok_step", "device": "lh",
             "capability": "read:tips_available"},
            {"name": "bad_step", "device": "lh", "capability": "proc:transfer",
             "args": {"source_barcode": "P1", "source_well": "H12",
                      "dest_barcode": "P1", "dest_well": "A2",
                      "volume": 100.0},
             "depends_on": ["ok_step"]}]}
        task = dispatch(ctx, "lab_tasks",
                        {"action": "add", "title": "will fail",
                         "protocol": proto}, principal=HUMAN)["result"]["task"]
        out = dispatch(ctx, "run_protocol", {"task_id": task["id"]},
                       principal=OP)
        assert out["ok"] is False                      # WorkflowError surfaced
        t = ctx.memory.get_task(task["id"])
        assert t["status"] == "failed"
        assert t["has_checkpoint"]                     # resume after repair
