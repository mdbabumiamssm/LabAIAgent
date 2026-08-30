"""PubMed browser over the NCBI E-utilities API.

Design rules:

1. **PubMed only.** The browser can query exactly one database (``db=pubmed``);
   every record it returns therefore carries a PMID and is, by construction,
   indexed in PubMed/MEDLINE. There is no parameter through which an agent can
   point it at an arbitrary URL.
2. **Polite by default.** NCBI asks for <=3 requests/second without an API key
   and identification via ``tool``/``email``; both are enforced here, with a
   monotonic-clock minimum interval.
3. **Offline-testable and injectable.** All network I/O goes through one
   ``fetcher(url) -> bytes`` callable, so the full parsing/filter logic is
   unit-tested without touching the network, and a lab behind an egress
   proxy can substitute its own transport.
4. **Data, not instructions.** Abstracts and titles are third-party text.
   The tool result labels them as untrusted content; nothing in this layer
   ever executes or interprets them.

Reference: NCBI E-utilities, https://www.ncbi.nlm.nih.gov/books/NBK25501/
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import LabAIAgentError

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class LiteratureUnavailable(LabAIAgentError):
    """The literature service could not be reached; carry on without it."""


@dataclass(frozen=True)
class Article:
    pmid: str
    title: str
    journal: str
    year: str
    authors: tuple[str, ...] = ()
    doi: str = ""
    abstract: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pmid": self.pmid, "title": self.title, "journal": self.journal,
            "year": self.year, "authors": list(self.authors), "doi": self.doi,
            "abstract": self.abstract,
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/",
        }


def _default_fetcher(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "labaiagent"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except Exception as exc:
        raise LiteratureUnavailable(
            f"PubMed (NCBI E-utilities) is unreachable: {exc}. Literature "
            f"search is unavailable; the lab itself is unaffected.") from exc


@dataclass
class PubMedBrowser:
    """Minimal, well-behaved E-utilities client for db=pubmed."""

    tool: str = "labaiagent"
    email: str = ""
    api_key: str = ""
    min_interval_s: float = 0.34          # <=3 req/s, per NCBI etiquette
    fetcher: Callable[[str], bytes] = field(default=_default_fetcher,
                                            repr=False)
    _last_call: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- plumbing -----------------------------------------------------------

    def _get(self, endpoint: str, **params: str) -> bytes:
        params = {"db": "pubmed", "tool": self.tool, **params}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        url = f"{EUTILS}/{endpoint}?{urllib.parse.urlencode(params)}"
        with self._lock:
            wait = self.min_interval_s - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        return self.fetcher(url)

    # -- API ----------------------------------------------------------------

    def search(self, query: str, *, max_results: int = 10,
               journal: str = "", reviews_only: bool = False,
               since_year: int | None = None) -> list[Article]:
        """Search PubMed and return summaries (no abstracts; see fetch_abstract).

        ``journal`` narrows to one journal title; ``reviews_only`` restricts
        to review articles; ``since_year`` sets a publication-date floor.
        """
        if not query.strip():
            raise LabAIAgentError("Empty literature query.")
        max_results = max(1, min(int(max_results), 50))
        term = query.strip()
        if journal.strip():
            term += f' AND "{journal.strip()}"[Journal]'
        if reviews_only:
            term += " AND Review[Publication Type]"
        if since_year:
            term += f' AND ("{int(since_year)}"[Date - Publication] : "3000"[Date - Publication])'

        raw = self._get("esearch.fcgi", term=term, retmax=str(max_results),
                        retmode="json", sort="relevance")
        try:
            ids = json.loads(raw)["esearchresult"].get("idlist", [])
        except (json.JSONDecodeError, KeyError) as exc:
            raise LiteratureUnavailable(
                f"PubMed returned an unparseable search response: {exc}") from exc
        if not ids:
            return []
        return self.summaries(ids)

    def summaries(self, pmids: list[str]) -> list[Article]:
        raw = self._get("esummary.fcgi", id=",".join(pmids), retmode="json")
        try:
            result = json.loads(raw)["result"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise LiteratureUnavailable(
                f"PubMed returned an unparseable summary response: {exc}") from exc
        out: list[Article] = []
        for pmid in pmids:
            rec = result.get(pmid)
            if not isinstance(rec, dict):
                continue
            doi = next((a.get("value", "") for a in rec.get("articleids", [])
                        if a.get("idtype") == "doi"), "")
            out.append(Article(
                pmid=pmid,
                title=(rec.get("title") or "").strip(),
                journal=(rec.get("fulljournalname")
                         or rec.get("source") or "").strip(),
                year=(rec.get("pubdate") or "").split(" ")[0],
                authors=tuple(a.get("name", "")
                              for a in rec.get("authors", [])[:8]),
                doi=doi,
            ))
        return out

    def fetch_abstract(self, pmid: str) -> str:
        """Plain-text abstract for one PMID (untrusted third-party text)."""
        pmid = str(pmid).strip()
        if not pmid.isdigit():
            raise LabAIAgentError(f"{pmid!r} is not a PMID (digits only).")
        raw = self._get("efetch.fcgi", id=pmid, rettype="abstract",
                        retmode="text")
        return raw.decode("utf-8", "replace").strip()


__all__ = ["PubMedBrowser", "Article", "LiteratureUnavailable"]
