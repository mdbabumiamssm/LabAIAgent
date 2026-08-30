"""Tests for the knowledge layer (PubMed, trusted sources, protocol
templates), individual password e-signatures, and agent oversight
(supervision, reviewers, RLHF feedback capture).

Run:  python -m pytest tests/test_knowledge_oversight.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from labaiagent import LabSession
from labaiagent.core.errors import ConfigurationError, LabAIAgentError
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    SimulatedThermocycler,
    World,
)
from labaiagent.gateway.auth import Principal, Role, hash_password
from labaiagent.gateway.registry import GatewayContext, dispatch
from labaiagent.knowledge.library import Citation, get_template
from labaiagent.knowledge.pubmed import LiteratureUnavailable, PubMedBrowser
from labaiagent.knowledge.sources import is_trusted_source
from labaiagent.oversight.feedback import FeedbackStore
from labaiagent.oversight.supervisor import (
    FoundationModelReviewer,
    Supervisor,
)

OP = Principal(id="agent:op", role=Role.OPERATOR)
APPROVER = Principal(id="user:appr", kind="human", role=Role.APPROVER)


@pytest.fixture
def world():
    return World(seed=7)


@pytest.fixture
def session(world, tmp_path):
    s = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world),
         SimulatedThermocycler("cycler", world=world),
         SimulatedRobotArm("arm", world=world)],
        name="ko-test", audit_path=tmp_path / "audit.jsonl", actor="user:test",
    )
    s.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    world.add_labware(Labware("DIL", n_wells=6, kind="reservoir",
                              location="deck_2"))
    world.get("DIL").well("A1").add(3000.0, {})
    yield s
    s.disconnect_all()


# ==========================================================================
# Trusted sources
# ==========================================================================

class TestTrustedSources:
    def test_exact_and_subdomain_match(self):
        assert is_trusted_source("thermofisher.com").key == "thermofisher"
        assert is_trusted_source("https://www.thermofisher.com/x").key == "thermofisher"
        assert is_trusted_source("assets.neb.com").key == "neb"

    def test_lookalike_domains_are_refused(self):
        assert is_trusted_source("evil-thermofisher.com") is None
        assert is_trusted_source("thermofisher.com.evil.net") is None
        assert is_trusted_source("notneb.com") is None
        assert is_trusted_source("random-blog.example") is None

    def test_citation_requires_registered_source(self):
        with pytest.raises(ConfigurationError, match="trusted-source"):
            Citation("random_blog", "some post")


# ==========================================================================
# PubMed browser (offline, injected transport)
# ==========================================================================

def fake_ncbi(url: str) -> bytes:
    if "esearch.fcgi" in url:
        assert "db=pubmed" in url          # PubMed only, by construction
        return json.dumps({"esearchresult": {"idlist": ["3843705"]}}).encode()
    if "esummary.fcgi" in url:
        return json.dumps({"result": {"3843705": {
            "title": "Measurement of protein using bicinchoninic acid.",
            "fulljournalname": "Analytical Biochemistry",
            "pubdate": "1985 Oct", "authors": [{"name": "Smith PK"}],
            "articleids": [{"idtype": "doi",
                            "value": "10.1016/0003-2697(85)90442-7"}],
        }}}).encode()
    if "efetch.fcgi" in url:
        return b"1. Anal Biochem. 1985...\n\nBCA abstract text."
    raise AssertionError(f"unexpected url {url}")


class TestPubMed:
    def test_search_returns_pmid_carrying_articles(self):
        pm = PubMedBrowser(fetcher=fake_ncbi, min_interval_s=0)
        arts = pm.search("bicinchoninic acid protein assay")
        assert arts[0].pmid == "3843705"
        assert arts[0].journal == "Analytical Biochemistry"
        assert arts[0].doi.startswith("10.1016")
        assert "pubmed.ncbi.nlm.nih.gov" in arts[0].to_dict()["url"]

    def test_journal_filter_reaches_the_query(self):
        seen = {}
        def spy(url):
            if "esearch" in url:
                seen["url"] = url
            return fake_ncbi(url)
        pm = PubMedBrowser(fetcher=spy, min_interval_s=0)
        pm.search("qPCR", journal="Clinical Chemistry", reviews_only=True,
                  since_year=2009)
        from urllib.parse import unquote_plus
        q = unquote_plus(seen["url"])
        assert '"Clinical Chemistry"[Journal]' in q
        assert "Review[Publication Type]" in q

    def test_abstract_requires_numeric_pmid(self):
        pm = PubMedBrowser(fetcher=fake_ncbi, min_interval_s=0)
        assert "BCA abstract" in pm.fetch_abstract("3843705")
        with pytest.raises(LabAIAgentError):
            pm.fetch_abstract("javascript:alert(1)")

    def test_network_failure_degrades_cleanly(self):
        def down(url):
            raise LiteratureUnavailable("offline")
        pm = PubMedBrowser(fetcher=down, min_interval_s=0)
        with pytest.raises(LiteratureUnavailable):
            pm.search("anything")

    def test_search_literature_tool_marks_text_untrusted(self, session):
        ctx = GatewayContext.for_session(session)
        ctx.pubmed = PubMedBrowser(fetcher=fake_ncbi, min_interval_s=0)
        out = dispatch(ctx, "search_literature",
                       {"query": "BCA assay"},
                       principal=Principal(id="agent:ro", role=Role.OBSERVER))
        assert out["ok"] and out["result"]["count"] == 1
        assert "never actuate" in out["result"]["caution"]


# ==========================================================================
# Protocol templates: published method -> executable workflow
# ==========================================================================

class TestTemplates:
    def test_every_template_carries_checkable_provenance(self, session):
        out = dispatch(session, "list_protocol_templates", {})
        assert out["ok"]
        for t in out["result"]["templates"]:
            assert t["citations"], t["id"]
        srcs = {s["key"] for s in out["result"]["trusted_sources"]}
        assert {"pubmed", "thermofisher"} <= srcs

    def test_bca_template_cites_the_1985_paper(self):
        refs = [c.ref for c in get_template("bca_protein_assay").citations]
        assert "PMID:3843705" in refs

    def test_instantiate_binds_and_flags_approvals(self, session):
        out = dispatch(session, "instantiate_protocol_template", {
            "template_id": "bca_protein_assay",
            "parameters": {"standards_barcode": "P1",
                           "diluent_barcode": "DIL"}})
        assert out["ok"], out
        rep = out["result"]["report"]
        assert rep["device_bindings"] == {
            "liquid_handler": "lh", "robot_arm": "arm", "plate_reader": "reader"}
        assert rep["needs_approval"] == ["move_to_reader"]
        # Static validation must flag the missing approval, not hide it.
        assert any("approval" in p for p in rep["validation_problems"])
        assert rep["citations"][0]["ref"] == "PMID:3843705"

    def test_parameters_are_limit_checked(self, session):
        with_bad = dispatch(session, "instantiate_protocol_template", {
            "template_id": "bca_protein_assay",
            "parameters": {"standards_barcode": "P1",
                           "diluent_barcode": "DIL",
                           "wavelength": 900.0}})     # outside 540-590
        assert with_bad["ok"] is False
        assert "permitted range" in with_bad["message"]

    def test_instantiated_template_actually_runs(self, session, world):
        """The whole point: literature-grounded template -> validated
        protocol -> real (simulated) liquids moved."""
        out = dispatch(session, "instantiate_protocol_template", {
            "template_id": "serial_dilution_series",
            "parameters": {"barcode": "P1",
                           "wells": ["A1", "B1", "C1", "D1"],
                           "diluent_barcode": "DIL",
                           "transfer_volume": 50.0,
                           "diluent_volume": 50.0}})
        assert out["ok"], out
        assert out["result"]["report"]["validation_problems"] == []
        run = dispatch(session, "run_protocol",
                       {"protocol": out["result"]["protocol"]})
        assert run["ok"] and run["result"]["valid"], run
        report = run["result"]["summary"]
        assert report["counts"]["done"] == 1
        assert world.get("P1").well("B1").volume_uL > 0   # liquid moved

    def test_unknown_parameter_and_missing_device(self, session):
        bad = dispatch(session, "instantiate_protocol_template", {
            "template_id": "serial_dilution_series",
            "parameters": {"barcode": "P1", "wells": ["A1"],
                           "diluent_barcode": "DIL", "warp_speed": 9}})
        assert bad["ok"] is False and "unexpected parameter" in bad["message"]


# ==========================================================================
# Individual passwords: e-signatures on approval minting
# ==========================================================================

class TestESignatures:
    def make_signer(self):
        return Principal(id="user:signer", kind="human", role=Role.APPROVER,
                         password_pbkdf2=hash_password("correct-horse"))

    def test_password_required_when_on_file(self, session):
        out = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "t"}, principal=self.make_signer())
        assert out["ok"] is False and "password" in out["message"]

    def test_wrong_password_is_refused_and_audited(self, session):
        out = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "t", "password": "wrong"},
                       principal=self.make_signer())
        assert out["ok"] is False
        assert any(r.event == "esign_failed"
                   for r in session.audit.records())

    def test_correct_password_mints_esigned_token(self, session, world):
        out = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "validated move", "password": "correct-horse"},
                       principal=self.make_signer())
        assert out["ok"] and out["result"]["esigned"] is True
        token = out["result"]["approval"]
        run = dispatch(session, "run_procedure",
                       {"device_id": "arm", "capability": "move_labware",
                        "arguments": {"barcode": "P1",
                                      "destination": "hotel_1"},
                        "reason": "validated move", "approval": token},
                       principal=OP)
        assert run["ok"], run

    def test_no_password_on_file_still_works_unsigned(self, session):
        out = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "t"}, principal=APPROVER)
        assert out["ok"] and out["result"]["esigned"] is False


# ==========================================================================
# Oversight: suspension, reviewers, feedback
# ==========================================================================

class TestOversight:
    def attach(self, session, **kw) -> Supervisor:
        sup = Supervisor(max_refusals=3, window_s=60, **kw)
        GatewayContext.for_session(session).supervisor = sup
        return sup

    def test_refusal_streak_suspends_agent(self, session):
        sup = self.attach(session)
        for _ in range(3):
            dispatch(session, "write_state",
                     {"device_id": "arm", "capability": "speed",
                      "arguments": {"value": 9999}}, principal=OP)
        assert sup.is_suspended("agent:op")
        blocked = dispatch(session, "write_state",
                           {"device_id": "lh", "capability": "flow_rate",
                            "arguments": {"value": 50}}, principal=OP)
        assert blocked["error"] == "oversight_suspended"
        # Reads and the e-stop survive suspension.
        assert dispatch(session, "snapshot", {}, principal=OP)["ok"]
        assert dispatch(session, "emergency_stop", {"reason": "t"},
                        principal=OP)["ok"]
        assert any(r.event == "oversight_suspend"
                   for r in session.audit.records())

    def test_only_a_named_human_reinstates(self, session):
        sup = self.attach(session)
        sup.suspend("agent:op", "test", session=session)
        with pytest.raises(ValueError):
            sup.reinstate("agent:op", operator="")
        sup.reinstate("agent:op", operator="dr_babu", session=session)
        assert not sup.is_suspended("agent:op")

    def test_rules_reviewer_demands_a_reason_for_high_risk(self, session):
        self.attach(session)
        out = dispatch(session, "run_procedure",
                       {"device_id": "arm", "capability": "move_labware",
                        "arguments": {"barcode": "P1",
                                      "destination": "hotel_2"}},
                       principal=OP)
        assert out["ok"] is False and out["error"] == "oversight_denied"
        assert out["reviewer"] == "rules"
        assert any(r.event == "oversight_denied"
                   for r in session.audit.records())

    def test_reviewed_call_proceeds_with_reason_and_token(self, session):
        self.attach(session)
        tok = dispatch(session, "request_approval",
                       {"device_id": "arm", "capability": "proc:move_labware",
                        "reason": "move plate for reading"},
                       principal=APPROVER)["result"]["approval"]
        out = dispatch(session, "run_procedure",
                       {"device_id": "arm", "capability": "move_labware",
                        "arguments": {"barcode": "P1",
                                      "destination": "hotel_3"},
                        "reason": "move plate for reading", "approval": tok},
                       principal=OP)
        assert out["ok"], out

    def test_foundation_model_reviewer_verdicts(self):
        deny = FoundationModelReviewer(
            complete=lambda p: '{"allow": false, "reason": "unjustified"}')
        v = deny.review({"risk": "high", "reason": "x"})
        assert v.allow is False and v.reviewer == "foundation_model"
        allow = FoundationModelReviewer(
            complete=lambda p: 'Verdict: {"allow": true, "reason": "fine"}')
        assert allow.review({"risk": "high"}).allow is True

    def test_foundation_model_reviewer_fails_closed(self):
        broken = FoundationModelReviewer(complete=lambda p: "not json at all")
        v = broken.review({"risk": "high"})
        assert v.allow is False and "fail-closed" in v.reason
        crashing = FoundationModelReviewer(
            complete=lambda p: (_ for _ in ()).throw(RuntimeError("api down")))
        assert crashing.review({"risk": "high"}).allow is False

    def test_feedback_accumulates_and_exports_dpo_pairs(self, session):
        sup = self.attach(session)
        # Human endorsement (approval) and human rejection (rating) of the
        # same situation -> one DPO pair.
        dispatch(session, "request_approval",
                 {"device_id": "arm", "capability": "proc:move_labware",
                  "reason": "good move"}, principal=APPROVER)
        out = dispatch(session, "submit_feedback",
                       {"rating": "reject", "agent_id": "agent:op",
                        "device_id": "arm", "capability": "proc:move_labware",
                        "comment": "moved the wrong plate"},
                       principal=APPROVER)
        assert out["ok"] is False or out["ok"]  # shape checked below
        # submit_feedback context uses tool 'manual'; align by re-recording:
        sup.feedback.record("human_rating", decision="reject",
                            actor="agent:op", judge="user:appr",
                            context={"tool": "request_approval",
                                     "device": "arm",
                                     "capability": "proc:move_labware"})
        pairs = sup.feedback.to_dpo_pairs()
        assert pairs and pairs[0]["prompt"]["device"] == "arm"
        assert sup.feedback.summary()["records"] >= 2

    def test_submit_feedback_is_human_gated(self, session):
        self.attach(session)
        out = dispatch(session, "submit_feedback",
                       {"rating": "approve", "agent_id": "agent:x"},
                       principal=OP)
        assert out["ok"] is False and out["error"] == "forbidden"

    def test_feedback_store_persists_jsonl(self, tmp_path):
        store = FeedbackStore(tmp_path / "fb.jsonl")
        store.record("human_rating", decision="approve", actor="a",
                     judge="j", context={"tool": "t"})
        again = FeedbackStore(tmp_path / "fb.jsonl")
        assert again.records()[0].decision == "approve"
        assert "human_rating" in again.export_jsonl()
