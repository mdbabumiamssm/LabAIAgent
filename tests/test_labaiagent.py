"""Test suite for LabAIAgent.

Run:  python -m pytest tests/ -v
      python tests/test_labaiagent.py      (no pytest required)

The safety tests carry the most weight. Everything else is a convenience; the
safety layer is the thing standing between a language model and a moving
robot arm, and it is tested for what it *refuses*, not what it permits.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from labaiagent import Device, LabSession, Param, Range, Risk, read
from labaiagent.core.audit import AuditLog
from labaiagent.core.capability import qualified, split_key
from labaiagent.core.conformance import verify_driver
from labaiagent.core.errors import (
    CapabilityNotFound,
    ConfirmationRequired,
    DeviceNotFound,
    DriverError,
    EmergencyStopActive,
    InterlockFailure,
    LimitViolation,
    SafetyViolation,
)
from labaiagent.core.types import MISSING, Kind, OneOf, Pattern, Quantity, convert
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedCentrifuge,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    SimulatedThermocycler,
    World,
    compute_ct,
)
from labaiagent.mcp.server import TOOLS, dispatch
from labaiagent.orchestration.workflow import OnError, Protocol

# ==========================================================================
# Fixtures
# ==========================================================================

@pytest.fixture
def world():
    return World(seed=1234)


@pytest.fixture
def session(world, tmp_path):
    s = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world),
         SimulatedThermocycler("cycler", world=world),
         SimulatedRobotArm("arm", world=world),
         SimulatedCentrifuge("spinner", world=world)],
        name="test-lab", audit_path=tmp_path / "audit.jsonl",
        actor="user:test",
    )
    s.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    yield s
    s.disconnect_all()


# ==========================================================================
# Types and units
# ==========================================================================

class TestTypes:
    def test_unit_conversion(self):
        assert convert(1.0, "mL", "uL") == 1000.0
        assert convert(1.0, "min", "s") == 60.0
        assert convert(0.0, "degC", "K") == 273.15

    def test_incommensurable_units_rejected(self):
        with pytest.raises(ValueError):
            convert(1.0, "uL", "degC")

    def test_unknown_unit_rejected_eagerly(self):
        with pytest.raises(ValueError, match="Unknown unit"):
            Quantity(1.0, "furlongs")

    def test_quantity_comparison_is_unit_aware(self):
        assert Quantity(1.0, "mL").to("uL").value == 1000.0

    def test_range_accepts_quantity_in_other_units(self):
        p = Param("v", float, "uL", limits=Range(1.0, 100.0, "uL"))
        assert p.validate(Quantity(0.05, "mL")) == pytest.approx(50.0)

    def test_bool_not_coerced_to_number(self):
        p = Param("v", float, "uL", limits=Range(0, 10, "uL"))
        with pytest.raises(TypeError):
            p.validate(True)

    def test_nonfinite_rejected(self):
        p = Param("v", float, "uL")
        with pytest.raises(ValueError):
            p.validate(float("inf"))

    def test_missing_sentinel_survives_deepcopy(self):
        import copy
        p = Param("v", float, "uL")
        assert copy.deepcopy(p).default is MISSING
        assert copy.deepcopy(p).required

    def test_pattern_and_oneof(self):
        assert Param("w", str, limits=Pattern(r"[A-H]\d+")).validate("A1") == "A1"
        with pytest.raises(ValueError):
            Param("w", str, limits=Pattern(r"[A-H]\d+")).validate("Z9x")
        with pytest.raises(ValueError):
            Param("m", str, limits=OneOf("a", "b")).validate("c")


# ==========================================================================
# Capability keying
# ==========================================================================

class TestCapabilityKeying:
    def test_read_and_write_may_share_a_name(self, session):
        lh = session.get("lh")
        assert lh.capability("read:flow_rate").kind is Kind.READ
        assert lh.capability("write:flow_rate").kind is Kind.WRITE

    def test_ambiguous_plain_name_refuses_to_guess(self, session):
        with pytest.raises(CapabilityNotFound, match="ambiguous"):
            session.get("lh").capability("flow_rate")

    def test_unambiguous_plain_name_resolves(self, session):
        assert session.get("lh").capability("transfer").kind is Kind.PROCEDURE

    def test_unknown_capability_suggests_alternatives(self, session):
        with pytest.raises(CapabilityNotFound) as exc:
            session.get("lh").capability("transfr")
        assert "transfer" in str(exc.value)

    def test_split_and_qualify_roundtrip(self):
        assert split_key(qualified(Kind.WRITE, "x")) == (Kind.WRITE, "x")
        assert split_key("plain") == (None, "plain")

    def test_read_helper_cannot_smuggle_a_write(self, session):
        """read() strips any prefix, so a 'write:' name resolves to the READ."""
        before = session.read("lh", "flow_rate")
        assert session.read("lh", "write:flow_rate") == before  # observed, not set
        assert session.read("lh", "flow_rate") == before        # nothing actuated


# ==========================================================================
# Driver contract
# ==========================================================================

class TestDriverContract:
    def test_all_builtin_drivers_pass_strict_conformance(self, world):
        for cls in (SimulatedLiquidHandler, SimulatedPlateReader,
                    SimulatedThermocycler, SimulatedRobotArm, SimulatedCentrifuge):
            dev = cls("probe", world=world)
            rep = verify_driver(dev)
            assert rep.passed_at("strict"), rep.render()

    def test_driver_without_identity_is_rejected(self):
        class Nameless(Device):
            @read("x", unit="count")
            def get_x(self) -> int:
                return 1

            def _connect(self): pass
            def _disconnect(self): pass

        with pytest.raises(DriverError, match="vendor"):
            Nameless("d")

    def test_driver_with_no_capabilities_is_rejected(self):
        class Empty(Device):
            vendor, model = "A", "B"

            def _connect(self): pass
            def _disconnect(self): pass

        with pytest.raises(DriverError, match="no capabilities"):
            Empty("d")

    def test_read_declared_risky_is_rejected(self):
        class BadRead(Device):
            vendor, model = "A", "B"

            @read("x", unit="count", risk=Risk.HIGH)
            def get_x(self) -> int:
                return 1

            def _connect(self): pass
            def _disconnect(self): pass

        with pytest.raises(DriverError, match="risk=NONE"):
            BadRead("d")

    def test_manifest_is_json_serialisable(self, session):
        json.dumps(session.manifest())

    def test_reference_sheet_covers_every_capability(self, session):
        for dev in session.devices():
            sheet = dev.reference_sheet()
            for cap in dev.capabilities.values():
                assert cap.name in sheet


# ==========================================================================
# Config-driven limit refinement
# ==========================================================================

class TestConfigRefinement:
    def test_config_may_narrow_a_limit(self, world):
        tc = SimulatedThermocycler(
            "tc", world=world,
            config={"limits": {"write:block_temperature":
                               {"value": {"low": 20, "high": 60, "unit": "degC"}}}})
        with pytest.raises(LimitViolation):
            tc.capability("write:block_temperature").validate_args({"value": 90.0})

    def test_config_may_not_widen_a_limit(self, world):
        with pytest.raises(DriverError, match="only narrow"):
            SimulatedThermocycler(
                "tc", world=world,
                config={"limits": {"write:block_temperature":
                                   {"value": {"low": -100, "high": 500,
                                              "unit": "degC"}}}})

    def test_config_may_raise_but_not_lower_risk(self, world):
        arm = SimulatedRobotArm("a", world=world,
                                config={"risk": {"write:speed": "critical"}})
        assert arm.capability("write:speed").risk is Risk.CRITICAL
        with pytest.raises(DriverError, match="cannot lower risk"):
            SimulatedRobotArm("b", world=world,
                              config={"risk": {"proc:move_labware": "low"}})

    def test_config_cannot_disable_confirmation(self, world):
        with pytest.raises(DriverError, match="cannot switch off"):
            SimulatedCentrifuge("c", world=world,
                                config={"requires_confirmation": {"proc:spin": False}})

    def test_unknown_capability_in_config_is_rejected(self, world):
        with pytest.raises(DriverError, match="unknown capability"):
            SimulatedRobotArm("a", world=world,
                              config={"limits": {"write:nonexistent": {}}})


# ==========================================================================
# Safety -- the tests that matter most
# ==========================================================================

class TestSafety:
    def test_out_of_range_write_is_refused(self, session):
        with pytest.raises(LimitViolation) as exc:
            session.write("arm", "speed", value=500.0)
        assert exc.value.permitted
        assert exc.value.parameter == "value"

    def test_error_carries_repair_information(self, session):
        with pytest.raises(LimitViolation) as exc:
            session.write("arm", "speed", value=500.0)
        d = exc.value.to_dict()
        assert d["value"] == 500.0
        assert "5" in d["permitted"] and "100" in d["permitted"]

    def test_missing_argument_names_the_parameter(self, session):
        with pytest.raises(LimitViolation) as exc:
            session.run("lh", "transfer", source_barcode="P1", source_well="A1",
                        dest_barcode="P1", dest_well="B1")
        assert exc.value.parameter == "volume"

    def test_unknown_argument_is_refused(self, session):
        with pytest.raises(LimitViolation, match="unexpected"):
            session.write("arm", "speed", value=50.0, turbo=True)

    def test_high_risk_requires_approval(self, session):
        with pytest.raises(ConfirmationRequired):
            session.run("arm", "move_labware", barcode="P1", destination="hotel_1")

    def test_approval_permits_the_action(self, session):
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="test")
        out = session.run("arm", "move_labware", barcode="P1",
                          destination="hotel_1", approval=tok)
        assert out["to"] == "hotel_1"

    def test_approval_is_single_use(self, session):
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="test")
        session.run("arm", "move_labware", barcode="P1", destination="hotel_1",
                    approval=tok)
        with pytest.raises(ConfirmationRequired):
            session.run("arm", "move_labware", barcode="P1",
                        destination="hotel_2", approval=tok)

    def test_approval_is_scoped_to_device_and_capability(self, session):
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="test")
        with pytest.raises(ConfirmationRequired, match="scoped"):
            session.run("spinner", "spin", rcf=1000.0, duration=60.0, approval=tok)

    def test_approval_expires(self, session):
        tok = session.request_approval("arm", "proc:move_labware", operator="op",
                                       reason="t", ttl_s=-1.0)
        with pytest.raises(ConfirmationRequired, match="expired"):
            session.run("arm", "move_labware", barcode="P1",
                        destination="hotel_1", approval=tok)

    def test_interlock_blocks_occupied_destination(self, session, world):
        world.add_labware(Labware("P2", n_wells=96, location="hotel_3"))
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="t")
        with pytest.raises(InterlockFailure, match="destination_free"):
            session.run("arm", "move_labware", barcode="P1",
                        destination="hotel_3", approval=tok)

    def test_unbalanced_centrifuge_is_blocked(self, session):
        session.run("spinner", "load_bucket", bucket=1, barcode="P1")
        tok = session.request_approval("spinner", "proc:spin",
                                       operator="op", reason="t")
        with pytest.raises(InterlockFailure, match="balanced"):
            session.run("spinner", "spin", rcf=1000.0, duration=60.0, approval=tok)

    def test_capability_mutation_does_not_leak_between_instances(self, world):
        a = SimulatedRobotArm("a", world=world)
        b = SimulatedRobotArm("b", world=world)
        a.capability("proc:move_labware").interlocks = ("tampered",)
        assert b.capability("proc:move_labware").interlocks != ("tampered",)

    def test_refused_actions_are_audited(self, session):
        try:
            session.write("arm", "speed", value=9999.0)
        except SafetyViolation:
            pass
        refusals = [r for r in session.audit.records() if r.event == "refused"]
        assert refusals and refusals[-1].capability == "speed"
        assert "LimitViolation" in refusals[-1].error

    def test_unregistered_interlock_fails_closed(self, session):
        cap = session.get("arm").capability("proc:move_labware")
        cap.interlocks = ("no_such_interlock",)
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="t")
        with pytest.raises(InterlockFailure, match="not registered"):
            session.run("arm", "move_labware", barcode="P1",
                        destination="hotel_1", approval=tok)

    def test_raising_interlock_fails_closed(self, session):
        @session.define_interlock("explodes", "always raises")
        def _boom(sess, ctx):
            raise RuntimeError("kaboom")

        cap = session.get("arm").capability("proc:move_labware")
        cap.interlocks = ("explodes",)
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="t")
        with pytest.raises(InterlockFailure):
            session.run("arm", "move_labware", barcode="P1",
                        destination="hotel_1", approval=tok)

    def test_rate_limit_stops_runaway_loop(self, session):
        session.safety.set_rate_limit("lh.flow_rate", max_calls=2, window_s=60)
        session.write("lh", "flow_rate", value=50.0)
        session.write("lh", "flow_rate", value=50.0)
        with pytest.raises(SafetyViolation, match="Rate limit"):
            session.write("lh", "flow_rate", value=50.0)

    def test_estop_blocks_all_actuation(self, session):
        session.emergency_stop("test")
        with pytest.raises(EmergencyStopActive):
            session.write("lh", "flow_rate", value=50.0)

    def test_estop_still_permits_reads(self, session):
        session.emergency_stop("test")
        assert session.read("cycler", "block_temperature") is not None

    def test_estop_halts_every_device(self, session):
        session.emergency_stop("test")
        assert all(d.state.value == "estopped" for d in session.devices())

    def test_estop_reset_requires_operator_and_reason(self, session):
        session.emergency_stop("test")
        with pytest.raises(ValueError):
            session.safety.estop.reset(operator="", reason="")
        session.reset_emergency_stop(operator="op", reason="cleared")
        assert not session.safety.estop.active

    def test_dry_run_never_actuates(self, world, tmp_path):
        s = LabSession([SimulatedLiquidHandler("lh", world=world)],
                       audit_path=tmp_path / "a.jsonl", dry_run=True)
        s.connect_all()
        before = world.tips_used
        out = s.write("lh", "flow_rate", value=42.0)
        assert out["dry_run"] is True
        assert world.tips_used == before
        assert s.read("lh", "flow_rate") == 100.0   # unchanged


# ==========================================================================
# Audit
# ==========================================================================

class TestAudit:
    def test_actions_are_recorded(self, session):
        session.write("lh", "flow_rate", value=50.0)
        events = [r.event for r in session.audit.records()]
        assert "invoke" in events and "result" in events

    def test_chain_verifies_clean(self, session):
        session.write("lh", "flow_rate", value=50.0)
        ok, msg = session.audit.verify()
        assert ok, msg

    def test_tampering_is_detected(self, session, tmp_path):
        session.write("lh", "flow_rate", value=50.0)
        path = Path(session.audit.path)
        lines = path.read_text().splitlines()
        rec = json.loads(lines[2])
        rec["arguments"] = {"value": 99999}
        lines[2] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        bad = tmp_path / "tampered.jsonl"
        bad.write_text("\n".join(lines) + "\n")
        ok, msg = AuditLog(bad).verify()
        assert not ok and "modified" in msg

    def test_deletion_is_detected(self, session, tmp_path):
        for _ in range(3):
            session.write("lh", "flow_rate", value=50.0)
        lines = Path(session.audit.path).read_text().splitlines()
        del lines[3]
        bad = tmp_path / "gap.jsonl"
        bad.write_text("\n".join(lines) + "\n")
        ok, msg = AuditLog(bad).verify()
        assert not ok

    def test_refusals_are_recorded_with_the_attempted_value(self, session):
        try:
            session.write("arm", "speed", value=9999.0)
        except SafetyViolation:
            pass
        rec = [r for r in session.audit.records() if r.event == "refused"][-1]
        assert rec.arguments["value"] == 9999.0
        assert rec.risk == "medium"

    def test_approvals_record_the_operator(self, session):
        session.request_approval("arm", "proc:move_labware",
                                 operator="dr_who", reason="because")
        rec = [r for r in session.audit.records() if r.event == "approval_issued"]
        assert rec and "dr_who" in rec[-1].actor

    def test_large_results_are_summarised(self, session, world):
        world.add_labware(Labware("BIG", n_wells=96, location="reader_carriage"))
        for w in world.get("BIG").wells:
            world.get("BIG").well(w).add(200.0, {"protein": 50.0})
        session.run("reader", "read_absorbance", wavelength=562.0, barcode="BIG")
        rec = [r for r in session.audit.records() if r.event == "result"][-1]
        assert "_summary" in json.dumps(rec.result)


# ==========================================================================
# Session
# ==========================================================================

class TestSession:
    def test_unknown_device_suggests_alternatives(self, session):
        with pytest.raises(DeviceNotFound):
            session.get("nonexistent")

    def test_duplicate_device_id_rejected(self, session, world):
        from labaiagent.core.errors import LabAIAgentError
        with pytest.raises(LabAIAgentError):
            session.add(SimulatedLiquidHandler("lh", world=world))

    def test_snapshot_covers_every_device(self, session):
        snap = session.snapshot()
        assert set(snap) == {d.id for d in session.devices()}
        assert snap["arm"]["speed"] == 50.0

    def test_partial_connect_does_not_abort_the_lab(self, world, tmp_path):
        class Broken(SimulatedLiquidHandler):
            def _connect(self):
                raise RuntimeError("unplugged")

        s = LabSession([SimulatedLiquidHandler("good", world=world),
                        Broken("bad", world=world)],
                       audit_path=tmp_path / "a.jsonl")
        results = s.connect_all()
        assert results["good"] == "ok" and "unplugged" in results["bad"]

    def test_device_can_be_added_at_runtime(self, session, world):
        session.add(SimulatedPlateReader("reader2", world=world))
        assert "reader2" in session


# ==========================================================================
# Workflow
# ==========================================================================

class TestWorkflow:
    def test_validation_catches_unknown_device(self, session):
        p = Protocol("p").step("s", "ghost", "read:x")
        assert any("ghost" in x for x in p.validate(session))

    def test_validation_catches_bad_argument(self, session):
        p = Protocol("p").step("s", "arm", "write:speed", args={"value": 9999})
        assert any("outside permitted range" in x for x in p.validate(session))

    def test_validation_catches_missing_approval(self, session):
        p = Protocol("p").step("s", "arm", "proc:move_labware",
                               args={"barcode": "P1", "destination": "hotel_1"})
        assert any("approval" in x for x in p.validate(session))

    def test_validation_catches_dependency_cycle(self, session):
        p = Protocol("p")
        p.step("a", "lh", "read:tips_available", depends_on=("b",))
        p.step("b", "lh", "read:tips_available", depends_on=("a",))
        assert any("cycle" in x for x in p.validate(session))

    def test_context_passing_between_steps(self, session):
        p = Protocol("p")
        p.step("read_tips", "lh", "read:tips_available", store_as="tips")
        p.run(session)
        assert p.context["tips"] > 0

    def test_abort_stops_downstream_steps(self, session):
        p = Protocol("p")
        p.step("bad", "lh", "proc:transfer",
               args={"source_barcode": "P1", "source_well": "H12",
                     "dest_barcode": "P1", "dest_well": "A2", "volume": 100.0})
        p.step("after", "lh", "read:tips_available", depends_on=("bad",))
        from labaiagent.core.errors import WorkflowError
        with pytest.raises(WorkflowError):
            p.run(session, validate=False)
        assert p._index["after"].status.value == "skipped"

    def test_continue_on_error_proceeds(self, session):
        p = Protocol("p")
        p.step("bad", "lh", "proc:transfer",
               args={"source_barcode": "P1", "source_well": "H12",
                     "dest_barcode": "P1", "dest_well": "A2", "volume": 100.0},
               on_error=OnError.CONTINUE)
        p.step("after", "lh", "read:tips_available", depends_on=("bad",))
        p.run(session, validate=False)
        assert p._index["after"].status.value == "done"

    def test_checkpoint_resume_skips_completed_steps(self, session, tmp_path):
        cp = tmp_path / "cp.json"
        p = Protocol("p", checkpoint_path=cp)
        p.step("one", "lh", "read:tips_available")
        p.run(session)
        p2 = Protocol("p", checkpoint_path=cp)
        p2.step("one", "lh", "read:tips_available")
        assert p2.resume_from_checkpoint() == 1

    def test_roundtrip_through_dict(self, session):
        p = Protocol("p", description="d")
        p.step("s", "lh", "read:tips_available")
        p2 = Protocol.from_dict(p.to_dict())
        assert p2.validate(session) == []


# ==========================================================================
# MCP
# ==========================================================================

class TestMCP:
    def test_tool_count_is_fixed(self, session, world):
        n = len(TOOLS)
        session.add(SimulatedPlateReader("extra", world=world))
        assert len(TOOLS) == n

    def test_list_devices(self, session):
        out = dispatch(session, "list_devices", {})
        assert out["ok"] and out["result"]["count"] == 5

    def test_read_state_cannot_actuate(self, session):
        out = dispatch(session, "read_state",
                       {"device_id": "lh", "capability": "flow_rate"})
        assert out["ok"] and out["result"]["value"] == 100.0

    def test_safety_violation_returns_structured_payload(self, session):
        out = dispatch(session, "write_state",
                       {"device_id": "arm", "capability": "speed",
                        "arguments": {"value": 9999}})
        assert out["ok"] is False
        assert out["error"] == "LimitViolation"
        assert out["retryable"] is False
        assert "permitted" in out

    def test_readonly_mode_hides_actuating_tools(self, session):
        out = dispatch(session, "write_state",
                       {"device_id": "lh", "capability": "flow_rate",
                        "arguments": {"value": 50}}, readonly=True)
        assert out["ok"] is False and out["error"] == "unknown_tool"

    def test_unknown_tool_lists_alternatives(self, session):
        out = dispatch(session, "nope", {})
        assert out["ok"] is False and "list_devices" in out["available"]

    def test_protocol_validate_only_does_not_actuate(self, session, world):
        before = world.tips_used
        out = dispatch(session, "run_protocol", {
            "protocol": {"name": "p", "steps": [
                {"name": "s", "device": "lh", "capability": "read:tips_available"}]},
            "validate_only": True})
        assert out["ok"] and out["result"]["valid"]
        assert world.tips_used == before


# ==========================================================================
# Simulated physics
# ==========================================================================

class TestSimulation:
    def test_pipetting_error_is_worse_at_low_volume(self, world):
        lh = SimulatedLiquidHandler("lh", world=world)
        lh.connect()
        world.add_labware(Labware("S", n_wells=96, location="deck_1"))
        world.get("S").well("A1").add(10000.0, {"x": 1.0})
        big, small = [], []
        for i in range(30):
            dest = f"{chr(ord('B') + i % 7)}{1 + i % 12}"
            big.append(abs(lh.transfer("S", "A1", "S", dest, 20.0)["relative_error"]))
        for i in range(30):
            dest = f"{chr(ord('B') + i % 7)}{1 + i % 12}"
            small.append(abs(lh.transfer("S", "A1", "S", dest, 2.0)["relative_error"]))
        assert sum(small) / len(small) > sum(big) / len(big)

    def test_aspirating_more_than_present_is_refused(self, world):
        lh = SimulatedLiquidHandler("lh", world=world)
        lh.connect()
        world.add_labware(Labware("S", n_wells=96, location="deck_1"))
        world.get("S").well("A1").add(10.0, {})
        with pytest.raises(Exception, match="liquid-level"):
            lh.transfer("S", "A1", "S", "B1", 500.0)

    def test_qpcr_ct_decreases_with_starting_copies(self, session, world):
        plate = Labware("Q", n_wells=96, location="cycler_block")
        world.add_labware(plate)
        for well, copies in zip(["A1", "A2", "A3"], [1e6, 1e4, 1e2], strict=True):
            plate.well(well).add(20.0, {"template": copies})
        tok = session.request_approval("cycler", "proc:run_qpcr",
                                       operator="op", reason="t")
        session.write("cycler", "lid_closed", closed=True)
        out = session.run("cycler", "run_qpcr", barcode="Q", approval=tok)
        cts = out["ct"]
        assert cts["A1"] < cts["A2"] < cts["A3"]

    def test_ntc_gives_no_ct(self):
        from labaiagent.drivers.simulated.world import qpcr_curve
        assert compute_ct(qpcr_curve(0.0)) is None

    def test_moving_a_plate_updates_shared_world(self, session, world):
        tok = session.request_approval("arm", "proc:move_labware",
                                       operator="op", reason="t")
        session.run("arm", "move_labware", barcode="P1",
                    destination="reader_carriage", approval=tok)
        assert world.get("P1").location == "reader_carriage"
        assert session.read("reader", "carriage_occupied") is True

    def test_reader_refuses_when_carriage_empty(self, session):
        from labaiagent.core.errors import LabAIAgentError
        with pytest.raises(LabAIAgentError):   # interlock or physical refusal
            session.run("reader", "read_absorbance", wavelength=562.0)


def _run_without_pytest() -> int:
    """Minimal runner so the suite works on a machine with no pytest."""
    import inspect

    classes = [v for k, v in list(globals().items())
               if inspect.isclass(v) and k.startswith("Test")]
    passed = failed = 0
    failures = []
    for cls in classes:
        for name in dir(cls):
            if not name.startswith("test_"):
                continue
            w = World(seed=1234)
            tmp = Path(tempfile.mkdtemp())
            s = LabSession(
                [SimulatedLiquidHandler("lh", world=w),
                 SimulatedPlateReader("reader", world=w),
                 SimulatedThermocycler("cycler", world=w),
                 SimulatedRobotArm("arm", world=w),
                 SimulatedCentrifuge("spinner", world=w)],
                name="t", audit_path=tmp / "a.jsonl")
            s.connect_all()
            w.add_labware(Labware("P1", n_wells=96, location="deck_1"))
            w.get("P1").well("A1").add(200.0, {"protein": 100.0})
            fn = getattr(cls(), name)
            kwargs = {}
            for p in inspect.signature(fn).parameters:
                kwargs[p] = {"session": s, "world": w, "tmp_path": tmp}.get(p)
            try:
                fn(**kwargs)
                passed += 1
            except Exception as exc:
                failed += 1
                failures.append(f"{cls.__name__}.{name}: {type(exc).__name__}: {exc}")
            finally:
                s.disconnect_all()
    print(f"\n{passed} passed, {failed} failed")
    for f in failures:
        print("  FAIL", f)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        import pytest as _p
        raise SystemExit(_p.main([__file__, "-q"]))
    except ImportError:
        raise SystemExit(_run_without_pytest()) from None
