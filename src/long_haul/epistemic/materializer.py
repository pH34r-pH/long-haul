"""Opt-in event projection. Event history, not this graph, owns domain authority."""
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdflib import Graph, URIRef
from rdflib import Literal as RDFLiteral
from rdflib.namespace import DCTERMS, PROV, RDF, XSD

from ..events.store import Event
from .vocabulary import LH, PROJECTION, VERSION, identifier


class Annotation(BaseModel):
    """Bounded adapter input inside Event.payload['epistemic']; not a new log."""

    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(strict=True, ge=1, le=1)
    kind: Literal["claim", "evidence", "assumption", "contradiction", "retraction"]
    text: str | None = Field(default=None, min_length=1)
    supports: list[str] = Field(default_factory=list)
    negates: str | None = Field(default=None, min_length=1)
    targets: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def structural_fields(self) -> "Annotation":
        object_kind = self.kind in {"claim", "evidence", "assumption"}
        if object_kind != (self.text is not None):
            raise ValueError("only claims, evidence and assumptions require text")
        target_count = {"contradiction": 2, "retraction": 1}.get(self.kind, 0)
        if len(self.targets) != target_count or len(set(self.targets)) != target_count:
            raise ValueError("wrong number of distinct targets")
        if not object_kind and (self.supports or self.negates or self.artifacts):
            raise ValueError("relations cannot carry object fields")
        if self.negates is not None and self.kind != "claim":
            raise ValueError("only a claim can explicitly negate another claim")
        for values in (self.supports, self.targets, self.artifacts):
            if any(not value for value in values) or len(values) != len(set(values)):
                raise ValueError("references must be nonempty and unique")
        return self


def _provenance(graph: Graph, event: Event, node: URIRef, kind: str) -> None:
    origin = identifier("event", event.id)
    activity = identifier("activity", event.id)
    graph.add((origin, RDF.type, PROV.Entity))
    graph.add((origin, RDF.type, LH.AuthoritativeEvent))
    graph.add((origin, LH.eventId, RDFLiteral(event.id)))
    graph.add((origin, LH.eventSchemaVersion, RDFLiteral(event.schema_version)))
    graph.add((origin, LH.eventType, RDFLiteral(event.event_type)))
    graph.add((activity, RDF.type, PROV.Activity))
    graph.add((activity, PROV.used, origin))
    graph.add((node, RDF.type, LH[kind.capitalize()]))
    graph.add((node, RDF.type, PROV.Entity))
    graph.add((node, PROV.wasDerivedFrom, origin))
    graph.add((node, PROV.wasGeneratedBy, activity))
    graph.add((node, PROV.generatedAtTime, RDFLiteral(event.timestamp, datatype=XSD.dateTime)))
    if event.source is not None:
        graph.add((origin, DCTERMS.source, RDFLiteral(event.source)))
    if event.actor is not None:
        agent = identifier("agent", event.actor)
        graph.add((agent, RDF.type, PROV.Agent))
        graph.add((agent, DCTERMS.identifier, RDFLiteral(event.actor)))
        graph.add((activity, PROV.wasAssociatedWith, agent))
        graph.add((node, PROV.wasAttributedTo, agent))


def _objects(graph: Graph, event: Event, annotation: Annotation, known: dict[str, str]) -> None:
    node = identifier("object", event.id)
    _provenance(graph, event, node, annotation.kind)
    graph.add((node, DCTERMS.description, RDFLiteral(annotation.text)))
    for source_id in annotation.supports:
        if source_id not in known:
            raise ValueError(f"support references unknown earlier object: {source_id}")
        graph.add((node, LH.supports, identifier("object", source_id)))
    if annotation.negates is not None:
        if known.get(annotation.negates) != "claim":
            raise ValueError("negation must reference an earlier claim")
        graph.add((node, LH.negates, identifier("object", annotation.negates)))
    for fingerprint in annotation.artifacts:
        artifact = identifier("artifact", fingerprint)
        graph.add((artifact, RDF.type, PROV.Entity))
        graph.add((artifact, RDF.type, LH.Artifact))
        graph.add((artifact, LH.artifactFingerprint, RDFLiteral(fingerprint)))
        graph.add((node, PROV.wasDerivedFrom, artifact))
    known[event.id] = annotation.kind


def _relation(graph: Graph, event: Event, annotation: Annotation, known: dict[str, str]) -> None:
    if any(target not in known for target in annotation.targets):
        raise ValueError("relation must reference earlier materialized objects")
    node = identifier("object", event.id)
    _provenance(graph, event, node, annotation.kind)
    targets = [identifier("object", target) for target in annotation.targets]
    for target in targets:
        graph.add((node, LH.target, target))
    if annotation.kind == "contradiction":
        if any(known[target] not in {"claim", "assumption"} for target in annotation.targets):
            raise ValueError("contradiction requires claims or assumptions")
        graph.add((targets[0], LH.contradicts, targets[1]))
        graph.add((targets[1], LH.contradicts, targets[0]))
    else:
        graph.add((targets[0], PROV.wasInvalidatedBy, identifier("activity", event.id)))
        if not list(graph.objects(targets[0], PROV.invalidatedAtTime)):
            graph.add((targets[0], PROV.invalidatedAtTime, RDFLiteral(event.timestamp, datatype=XSD.dateTime)))


def materialize(events: Iterable[Event]) -> Graph:
    """Build a fresh RDF 1.1 graph from append order; never mutate events or stores.

    Unannotated events are ignored. Marked inputs fail closed on invalid versions,
    duplicate event IDs, naive timestamps, or dangling/forward references.
    """
    graph = Graph()
    graph.bind("lh", LH)
    graph.bind("prov", PROV)
    graph.bind("dcterms", DCTERMS)
    graph.add((PROJECTION, RDF.type, LH.Projection))
    graph.add((PROJECTION, LH.version, RDFLiteral(VERSION)))
    seen: set[str] = set()
    known: dict[str, str] = {}
    for event in events:
        if not event.id or event.id in seen:
            raise ValueError("event IDs must be nonempty and unique")
        seen.add(event.id)
        if "epistemic" not in event.payload:
            continue
        if event.schema_version != 1 or event.timestamp.utcoffset() is None:
            raise ValueError("marked events require schema 1 and aware timestamps")
        annotation = Annotation.model_validate(event.payload["epistemic"])
        if annotation.kind in {"claim", "evidence", "assumption"}:
            _objects(graph, event, annotation, known)
        else:
            _relation(graph, event, annotation, known)
    return graph
