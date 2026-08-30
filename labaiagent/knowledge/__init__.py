"""The knowledge layer: literature and protocols an agent may trust.

An agent planning bench work needs two things a control layer normally does
not provide: *provenance-gated literature* (what does the peer-reviewed
record actually say?) and *executable method templates* (how does a published
assay become steps this lab can run?). This package provides both, with the
same fail-closed posture as the rest of the system:

  - ``pubmed``   -- a PubMed browser over NCBI E-utilities. PubMed only:
                    every hit is by construction indexed in MEDLINE/PubMed.
  - ``sources``  -- the trust registry: PubMed-indexed journals and a curated
                    allowlist of reliable pharmaceutical / instrument-vendor
                    protocol publishers. Anything else is refused, loudly.
  - ``library``  -- citation-carrying protocol templates that translate into
                    validated, executable LabAIAgent Protocols bound to the
                    devices actually present in the lab.

The translation pipeline is deliberately human-in-the-loop: a template
instantiates to a protocol *document* for review and static validation; it
never runs anything by itself.
"""

from .library import PROTOCOL_TEMPLATES, ProtocolTemplate, get_template
from .pubmed import PubMedBrowser
from .sources import TRUSTED_SOURCES, is_trusted_source

__all__ = ["PubMedBrowser", "TRUSTED_SOURCES", "is_trusted_source",
           "ProtocolTemplate", "PROTOCOL_TEMPLATES", "get_template"]
