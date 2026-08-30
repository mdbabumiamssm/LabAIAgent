"""Literature -> workflow, e-signatures, and AI-agent oversight.

Demonstrates the v1.2.0 layers end to end, offline (the PubMed transport is
stubbed with a real record so no network is needed):

  A. Literature: PubMed-only search returns PMID-carrying records.
  B. Templates:  a citation-carrying template (BCA assay, Smith 1985,
                 PMID:3843705) instantiates against THIS lab -- parameters
                 limit-checked, device categories bound, approvals flagged.
  C. Execution:  a serial-dilution template becomes a validated protocol and
                 actually moves (simulated) liquid.
  D. E-signature: minting an approval requires the individual's password,
                 re-entered at the moment of signing.
  E. Oversight:  the rules reviewer vetoes an unjustified HIGH-risk call; a
                 refusal streak gets the agent suspended; a human reinstates.
  F. RLHF:       every human judgement lands in the feedback store; the
                 DPO-ready dataset is exported.

Run:  python examples/05_knowledge_and_oversight.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from labaiagent import LabSession
from labaiagent.drivers.simulated import (
    Labware,
    SimulatedLiquidHandler,
    SimulatedPlateReader,
    SimulatedRobotArm,
    World,
)
from labaiagent.gateway.auth import Principal, Role, hash_password
from labaiagent.gateway.registry import GatewayContext, dispatch
from labaiagent.knowledge.pubmed import PubMedBrowser
from labaiagent.oversight.supervisor import Supervisor

AGENT = Principal(id="agent:claude", role=Role.OPERATOR)
DR_BABU = Principal(id="user:babu", kind="human", role=Role.APPROVER,
                    password_pbkdf2=hash_password("bench-2026"))


def rule(title: str) -> None:
    print("\n" + "=" * 74 + f"\n{title}\n" + "=" * 74)


def offline_pubmed(url: str) -> bytes:
    """Real PMID 3843705 metadata, served without network."""
    if "esearch" in url:
        return json.dumps({"esearchresult": {"idlist": ["3843705"]}}).encode()
    return json.dumps({"result": {"3843705": {
        "title": "Measurement of protein using bicinchoninic acid.",
        "fulljournalname": "Analytical Biochemistry", "pubdate": "1985 Oct",
        "authors": [{"name": "Smith PK"}],
        "articleids": [{"idtype": "doi",
                        "value": "10.1016/0003-2697(85)90442-7"}]}}}).encode()


def main() -> int:
    world = World(seed=12)
    session = LabSession(
        [SimulatedLiquidHandler("lh", world=world),
         SimulatedPlateReader("reader", world=world),
         SimulatedRobotArm("arm", world=world)],
        name="knowledge-demo", audit_path="runs/knowledge_audit.jsonl",
        actor="user:babu")
    session.connect_all()
    world.add_labware(Labware("P1", n_wells=96, location="deck_1"))
    world.get("P1").well("A1").add(200.0, {"protein": 100.0})
    world.add_labware(Labware("DIL", n_wells=6, kind="reservoir",
                              location="deck_2"))
    world.get("DIL").well("A1").add(3000.0, {})

    ctx = GatewayContext.for_session(session)
    ctx.pubmed = PubMedBrowser(fetcher=offline_pubmed, min_interval_s=0)
    ctx.supervisor = Supervisor(max_refusals=3, window_s=60)

    # ------------------------------------------------------------------ A
    rule("A. Literature: PubMed-indexed only, PMIDs attached")
    out = dispatch(ctx, "search_literature",
                   {"query": "bicinchoninic acid protein assay"},
                   principal=AGENT)
    art = out["result"]["articles"][0]
    print(f"  PMID {art['pmid']}: {art['title']}")
    print(f"  {art['journal']} ({art['year']})  doi:{art['doi']}")

    # ------------------------------------------------------------------ B
    rule("B. Template: published method -> this lab's instruments")
    out = dispatch(ctx, "instantiate_protocol_template", {
        "template_id": "bca_protein_assay",
        "parameters": {"standards_barcode": "P1",
                       "diluent_barcode": "DIL"}}, principal=AGENT)
    rep = out["result"]["report"]
    print(f"  citations      : {[c['ref'] for c in rep['citations']]}")
    print(f"  device bindings: {rep['device_bindings']}")
    print(f"  needs approval : {rep['needs_approval']}")

    # ------------------------------------------------------------------ C
    rule("C. Execute a fully-valid template: liquid actually moves")
    out = dispatch(ctx, "instantiate_protocol_template", {
        "template_id": "serial_dilution_series",
        "parameters": {"barcode": "P1", "wells": ["A1", "B1", "C1", "D1"],
                       "diluent_barcode": "DIL",
                       "transfer_volume": 50.0, "diluent_volume": 50.0}},
        principal=AGENT)
    run = dispatch(ctx, "run_protocol", {"protocol": out["result"]["protocol"]},
                   principal=AGENT)
    print(f"  validation: {out['result']['report']['validation_problems'] or 'clean'}")
    print(f"  run       : {run['result']['summary']['counts']}")
    print(f"  B1 volume : {world.get('P1').well('B1').volume_uL:.1f} uL "
          f"(was 0 -- the template did real work)")

    # ------------------------------------------------------------------ D
    rule("D. E-signature: the password is the person")
    no_pw = dispatch(ctx, "request_approval",
                     {"device_id": "arm", "capability": "proc:move_labware",
                      "reason": "move plate to reader"}, principal=DR_BABU)
    print(f"  without password -> {no_pw['message'][:64]}...")
    signed = dispatch(ctx, "request_approval",
                      {"device_id": "arm", "capability": "proc:move_labware",
                       "reason": "move plate to reader",
                       "password": "bench-2026"}, principal=DR_BABU)
    print(f"  with password    -> token minted, esigned="
          f"{signed['result']['esigned']}")

    # ------------------------------------------------------------------ E
    rule("E. Oversight: review, then suspension")
    vetoed = dispatch(ctx, "run_procedure",
                      {"device_id": "arm", "capability": "move_labware",
                       "arguments": {"barcode": "P1",
                                     "destination": "hotel_1"}},
                      principal=AGENT)   # HIGH risk, no reason given
    print(f"  reviewer veto: [{vetoed['error']}] {vetoed['message'][:58]}...")
    for _ in range(3):                    # the agent argues with the limits
        dispatch(ctx, "write_state",
                 {"device_id": "arm", "capability": "speed",
                  "arguments": {"value": 9999}}, principal=AGENT)
    blocked = dispatch(ctx, "write_state",
                       {"device_id": "lh", "capability": "flow_rate",
                        "arguments": {"value": 50}}, principal=AGENT)
    print(f"  after 3 refusals: {blocked['error']} "
          f"(reads and e-stop still work)")
    ctx.supervisor.reinstate("agent:claude", operator="user:babu",
                             session=session)
    print("  reinstated by a named human -- there is no tool for this.")

    # ------------------------------------------------------------------ F
    rule("F. RLHF: human judgements become a preference dataset")
    dispatch(ctx, "submit_feedback",
             {"rating": "reject", "agent_id": "agent:claude",
              "device_id": "arm", "capability": "proc:move_labware",
              "comment": "attempted move without justification"},
             principal=DR_BABU)
    fb = ctx.supervisor.feedback
    print(f"  dataset: {fb.summary()}")
    pairs = fb.to_dpo_pairs()
    if pairs:
        print(f"  first DPO pair prompt: {pairs[0]['prompt']}")
    print("  export via FeedbackStore.export_jsonl() / to_dpo_pairs(); "
          "training runs on YOUR infrastructure, not in this process.")

    session.disconnect_all()
    print("\nKnowledge with provenance. Signatures with identity. "
          "Oversight with teeth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
