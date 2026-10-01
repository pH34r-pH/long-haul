"""Offline JSON-LD 1.1 interchange and SHACL profile validation."""
import json
from importlib.resources import files

from pyld import jsonld
from pyshacl import validate
from rdflib import Graph, Literal

from .vocabulary import LH, PROJECTION, VERSION


class InvalidProjection(ValueError):
    """An RDF view fails the versioned structural profile."""


def validate_graph(graph: Graph) -> None:
    """Check packaged SHACL shapes without inference, imports or remote access."""
    shapes = Graph().parse(data=files(__package__).joinpath("shapes.ttl").read_text(), format="turtle")
    conforms, _, report = validate(graph, shacl_graph=shapes, inference="none")
    if not conforms:
        raise InvalidProjection(report)


def export_jsonld(graph: Graph) -> str:
    """Export a validated snapshot; serialization order is not semantic identity."""
    validate_graph(graph)
    return graph.serialize(format="json-ld", context={"@version": 1.1}, auto_compact=True)


def _offline_loader(url, options=None):
    raise ValueError(f"remote JSON-LD documents are not allowed: {url}")


def import_jsonld(document: str) -> Graph:
    """Import a graph snapshot, never events or authority. Reject remote contexts.

    PyLD provides explicit JSON-LD 1.1 processing; N-Triples parsing deliberately
    rejects named datasets, which are outside this single-graph profile.
    """
    triples = jsonld.to_rdf(json.loads(document), options={
        "format": "application/n-quads", "processingMode": "json-ld-1.1",
        "documentLoader": _offline_loader,
    })
    graph = Graph().parse(data=triples, format="nt")
    if list(graph.objects(PROJECTION, LH.version)) != [Literal(VERSION)]:
        raise InvalidProjection("missing or unsupported projection version")
    validate_graph(graph)
    return graph
