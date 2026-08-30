"""The trust registry: where lab-actionable knowledge may come from.

An agent that will move liquids must not take its methods from arbitrary
web pages. This module is the single gate: literature must be PubMed-indexed
(guaranteed by construction when it comes through the PubMed browser), and
protocol documents must come from a curated allowlist of publishers whose
technical documentation is professionally maintained -- major reagent and
instrument manufacturers, DOI-registered protocol repositories, and public
health agencies.

The list is deliberately conservative and deliberately editable: a lab adds
its own trusted sources in ONE place, in code, under review -- not by an
agent at run time. There is no API for an agent to extend this registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class TrustedSource:
    key: str
    name: str
    kind: str          # 'journal_index' | 'pharma_vendor' | 'protocol_repository' | 'public_agency'
    domains: tuple[str, ...]
    note: str = ""


#: Curated allowlist. Domains are matched by suffix on the registrable host.
TRUSTED_SOURCES: dict[str, TrustedSource] = {s.key: s for s in [
    TrustedSource(
        "pubmed", "PubMed / MEDLINE (NLM)", "journal_index",
        ("pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "nlm.nih.gov"),
        "Peer-reviewed literature index. The ONLY literature entry point "
        "this package exposes; every record has a PMID."),
    TrustedSource(
        "pmc", "PubMed Central", "journal_index",
        ("pmc.ncbi.nlm.nih.gov",),
        "Full text for PubMed-indexed open-access articles."),
    TrustedSource(
        "protocols_io", "protocols.io", "protocol_repository",
        ("protocols.io",),
        "DOI-registered, versioned protocol repository."),
    TrustedSource(
        "nature_protocols", "Nature Protocols / Protocol Exchange",
        "protocol_repository",
        ("nature.com",),
        "Peer-reviewed protocols; PubMed-indexed."),
    TrustedSource(
        "thermofisher", "Thermo Fisher Scientific", "pharma_vendor",
        ("thermofisher.com",),
        "Reagent/instrument documentation (e.g. Pierce BCA)."),
    TrustedSource(
        "sigmaaldrich", "MilliporeSigma / Merck", "pharma_vendor",
        ("sigmaaldrich.com", "merckmillipore.com", "emdmillipore.com")),
    TrustedSource(
        "neb", "New England Biolabs", "pharma_vendor", ("neb.com",)),
    TrustedSource(
        "qiagen", "QIAGEN", "pharma_vendor", ("qiagen.com",)),
    TrustedSource(
        "promega", "Promega", "pharma_vendor", ("promega.com",)),
    TrustedSource(
        "biorad", "Bio-Rad", "pharma_vendor", ("bio-rad.com",)),
    TrustedSource(
        "roche", "Roche Diagnostics / Life Science", "pharma_vendor",
        ("roche.com", "lifescience.roche.com",)),
    TrustedSource(
        "agilent", "Agilent", "pharma_vendor", ("agilent.com",)),
    TrustedSource(
        "beckman", "Beckman Coulter Life Sciences", "pharma_vendor",
        ("beckman.com",)),
    TrustedSource(
        "opentrons", "Opentrons Protocol Library", "protocol_repository",
        ("opentrons.com", "protocols.opentrons.com")),
    TrustedSource(
        "addgene", "Addgene", "protocol_repository", ("addgene.org",),
        "Nonprofit plasmid repository protocols."),
    TrustedSource(
        "nih", "NIH / NCI / NIAID", "public_agency", ("nih.gov",)),
    TrustedSource(
        "cdc", "CDC", "public_agency", ("cdc.gov",)),
    TrustedSource(
        "who", "WHO", "public_agency", ("who.int",)),
    TrustedSource(
        "fda", "FDA", "public_agency", ("fda.gov",)),
]}


def is_trusted_source(url_or_domain: str) -> TrustedSource | None:
    """Return the matching TrustedSource, or None (refuse) for anything else.

    Matching is by registrable-host suffix with a dot boundary, so
    ``evil-thermofisher.com`` and ``thermofisher.com.evil.net`` both fail.
    """
    raw = url_or_domain.strip().lower()
    host = urlparse(raw).netloc if "//" in raw else raw.split("/", 1)[0]
    host = host.split("@")[-1].split(":")[0]
    if not host:
        return None
    for source in TRUSTED_SOURCES.values():
        for dom in source.domains:
            if host == dom or host.endswith("." + dom):
                return source
    return None


def describe_sources() -> list[dict[str, str]]:
    return [{"key": s.key, "name": s.name, "kind": s.kind,
             "domains": ", ".join(s.domains), "note": s.note}
            for s in TRUSTED_SOURCES.values()]


__all__ = ["TrustedSource", "TRUSTED_SOURCES", "is_trusted_source",
           "describe_sources"]
