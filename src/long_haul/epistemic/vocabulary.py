"""Versioned projection vocabulary; identifiers are derived, never authoritative."""
from urllib.parse import quote

from rdflib import Namespace, URIRef

VERSION = 1
LH = Namespace("urn:long-haul:epistemic:v1:terms:")
PROJECTION = URIRef("urn:long-haul:epistemic:v1:projection")


def identifier(kind: str, source_id: str) -> URIRef:
    """Injectively encode exact authoritative identifiers without normalization."""
    return URIRef(f"urn:long-haul:epistemic:v1:{kind}:{quote(source_id, safe='')}")
