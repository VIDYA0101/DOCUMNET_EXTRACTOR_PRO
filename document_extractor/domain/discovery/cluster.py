"""Unsupervised document-type discovery.

Everything else in domain/matching/ answers "does this document match a
template I already have?" This module answers a different, harder question:
"group these documents by type, when no templates exist yet at all."

The naive approach - compare every new document against every type seen so
far - is the same O(n x k) trap TemplateMatcher already avoids for known
templates. It looks fine at 92 documents and 3 types. It does not look fine
at a million documents and a thousand types: that is roughly a billion
similarity comparisons, most of them wasted on types that were never going to
match. This module uses the identical fix - an inverted index over
distinctive tokens gives a handful of *candidate* types to actually compare
against, not all of them.

The second thing that breaks at that scale is holding everything in memory
and hoping the process does not get killed halfway through a run that takes
hours. So this is built to be incremental and resumable from the start: call
assign() once per document, in a batch-runner worker exactly like normal
processing already does, and persist the growing index the same file-only,
snapshot-plus-append-log way the rest of this application persists anything
that accumulates over time. There is no "cluster everything at once" mode
here on purpose - at real scale, that mode is not usable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import math

from ..matching.matcher import _l2, layout_signature, term_frequencies
from ..models import PdfDocument

_TYPE_KEYWORDS = [
    "TAX INVOICE", "CREDIT NOTE", "DEBIT NOTE", "PURCHASE ORDER",
    "DELIVERY NOTE", "DELIVERY CHALLAN", "STATEMENT OF ACCOUNT",
    "PROFORMA INVOICE", "RECEIPT", "QUOTATION", "BILL OF LADING",
    "PACKING LIST", "REMITTANCE ADVICE",
]

# Similarity above this -> same type as an existing cluster.
# Below -> distinct enough to be its own new type.
_JOIN_THRESHOLD = 0.55
# How many of a cluster's own distinctive tokens go into the shortlist index.
# More tokens means better recall (a document is more likely to be found as
# a candidate) at the cost of a slightly larger index - cheap to be generous
# here since the index itself, unlike full-text storage, stays small even at
# a thousand clusters.
_INDEX_TOKENS_PER_CLUSTER = 12

_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")
_STOPWORDS = {
    "the", "and", "for", "you", "our", "your", "with", "this", "that", "from",
    "all", "any", "are", "was", "will", "have", "has", "not", "per",
}


@dataclass(slots=True)
class DocSignature:
    """The cheap, fast-to-compute fingerprint of one document.

    Deliberately small: at a million documents this gets computed a million
    times and, for anything still open, held in memory many times over. A
    full per-document term-frequency dict already does this reasonably (a
    few dozen entries), so nothing heavier is added on top.
    """
    top_terms: dict[str, float]
    layout: list[float]
    doc_type_guess: str


def compute_signature(doc: PdfDocument) -> DocSignature:
    terms = term_frequencies(doc, pages=1)
    layout = layout_signature(doc)
    head = doc.pages[0].text.upper() if doc.pages else ""
    doc_type = next((kw for kw in _TYPE_KEYWORDS if kw in head), "UNKNOWN")
    return DocSignature(top_terms=terms, layout=layout, doc_type_guess=doc_type)


def _weighted_cosine(a: dict[str, float], b: dict[str, float],
                     idf_fn) -> float:
    """Cosine similarity where each shared term is weighted by its IDF -
    how rare it is across every document processed so far - rather than
    taken at face value.

    idf_fn is a callable, not a precomputed dict, and this matters at scale:
    a precomputed dict means rebuilding an entry for every term in the
    entire corpus vocabulary before every single comparison, which is its
    own O(vocabulary size) cost paid a million times over - exactly the
    class of mistake this module exists to avoid elsewhere. Computing IDF
    lazily, only for the handful of terms actually being compared, keeps
    this at O(shared terms) regardless of how large the vocabulary grows.

    This is the fix for the exact failure the stress test caught: without
    IDF weighting at all, two unrelated vendors' invoices score as highly
    similar purely because they share generic words like "invoice" and
    "total", which tells you nothing about which vendor a document actually
    came from. A word that appears in nearly every document contributes
    almost nothing here; a word that appears in only a handful of documents
    - the vendor's own name, a specific reference code - dominates, which
    is the correct way around.
    """
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    num = sum(a[k] * b[k] * (idf_fn(k) ** 2) for k in shared)
    da = math.sqrt(sum((v * idf_fn(k)) ** 2 for k, v in a.items()))
    db = math.sqrt(sum((v * idf_fn(k)) ** 2 for k, v in b.items()))
    return num / (da * db) if da and db else 0.0


def similarity(a: DocSignature, b: DocSignature, idf_fn) -> float:
    text_sim = _weighted_cosine(a.top_terms, b.top_terms, idf_fn)
    layout_sim = _l2(a.layout, b.layout)
    return 0.75 * text_sim + 0.25 * layout_sim


@dataclass(slots=True)
class Cluster:
    cluster_id: str
    doc_type_guess: str
    representative: DocSignature
    doc_count: int = 0
    sample_doc_ids: list[str] = field(default_factory=list)


class DiscoveryIndex:
    """Incremental, indexed clustering. Call assign() once per document.

    IDF weights are tracked online, updated after every document, because
    the whole corpus is never available upfront at the scale this is built
    for - a million documents means processing takes hours, in a resumable
    batch job, not one pass with the full picture in memory. This means
    early assignments are made with rougher IDF estimates than later ones;
    that is an accepted, self-correcting property of the online approach,
    not a bug - the alternative is not being able to start until every
    document has already been read once, which defeats the purpose.

    Persisting and restoring this - not covered in this module, which is
    intentionally storage-agnostic - follows the exact snapshot-plus-
    append-log shape already used for MemoryIndex: a rebuildable snapshot of
    self.clusters plus the doc-frequency counts, plus an append-only log of
    every assignment made, so a run can resume after being interrupted
    without losing what it had already learned.
    """

    def __init__(self):
        self.clusters: dict[str, Cluster] = {}
        self._token_index: dict[str, set[str]] = {}
        self._doc_frequency: dict[str, int] = {}
        self._total_docs = 0
        self._next_id = 1

    def _idf(self, term: str) -> float:
        df = self._doc_frequency.get(term, 0)
        return math.log((self._total_docs + 1) / (df + 1)) + 1.0

    def _distinctive_tokens(self, sig: DocSignature, limit: int) -> list[str]:
        """Rank this document's own terms by IDF, not raw frequency - the
        candidate index needs to be built from words that actually
        distinguish this document, which raw term frequency does not
        reliably identify (see the module docstring's stress-test finding)."""
        scored = [(term, sig.top_terms[term] * self._idf(term))
                  for term in sig.top_terms]
        scored.sort(key=lambda kv: -kv[1])
        return [t for t, _ in scored[:limit]]

    def _candidates(self, tokens: list[str], limit: int = 8) -> list[str]:
        """Stage 1: cheap shortlist via the inverted index - O(tokens), not
        O(clusters)."""
        hits: dict[str, int] = {}
        for tok in tokens:
            for cid in self._token_index.get(tok, ()):
                hits[cid] = hits.get(cid, 0) + 1
        return [cid for cid, _ in sorted(hits.items(), key=lambda kv: -kv[1])[:limit]]

    def assign(self, doc_id: str, sig: DocSignature) -> tuple[str, bool]:
        """Stage 2: verify only against the shortlist. Returns
        (cluster_id, is_new_cluster)."""
        tokens = self._distinctive_tokens(sig, _INDEX_TOKENS_PER_CLUSTER)
        idf_fn = self._idf   # bound once per document, not per comparison

        best_id, best_sim = None, 0.0
        for cid in self._candidates(tokens):
            cluster = self.clusters[cid]
            sim = similarity(sig, cluster.representative, idf_fn)
            if sim > best_sim:
                best_id, best_sim = cid, sim

        # Update document-frequency counts AFTER comparison, so a document
        # is never compared against a corpus statistic that already
        # includes itself.
        self._total_docs += 1
        for term in sig.top_terms:
            self._doc_frequency[term] = self._doc_frequency.get(term, 0) + 1

        if best_id is not None and best_sim >= _JOIN_THRESHOLD:
            cluster = self.clusters[best_id]
            cluster.doc_count += 1
            if len(cluster.sample_doc_ids) < 5:
                cluster.sample_doc_ids.append(doc_id)
            return best_id, False

        cid = f"type_{self._next_id:04d}"
        self._next_id += 1
        self.clusters[cid] = Cluster(cluster_id=cid, doc_type_guess=sig.doc_type_guess,
                                     representative=sig, doc_count=1,
                                     sample_doc_ids=[doc_id])
        for tok in tokens:
            self._token_index.setdefault(tok, set()).add(cid)
        return cid, True

    def summary(self) -> list[dict]:
        return [
            {"cluster_id": c.cluster_id, "doc_type_guess": c.doc_type_guess,
             "doc_count": c.doc_count, "sample_doc_ids": c.sample_doc_ids}
            for c in sorted(self.clusters.values(), key=lambda c: -c.doc_count)
        ]
