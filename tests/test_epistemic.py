"""Synthetic conformance corpus, not physical observations or truth inference."""
import json
from datetime import datetime
from pathlib import Path

import pytest
from pyld.jsonld import JsonLdError
from rdflib import Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.exceptions import ParserError
from rdflib.namespace import DCTERMS, PROV, RDF

from long_haul.epistemic import (
    LH,
    PROJECTION,
    InvalidProjection,
    export_jsonld,
    identifier,
    import_jsonld,
    materialize,
    validate_graph,
)
from long_haul.events.store import Event, EventStore

FIXTURES = Path(__file__).parent / "fixtures" / "epistemic"


def history():
    return EventStore(FIXTURES / "events.jsonl").events()


def test_deletion_rebuild_and_standard_fixture(tmp_path):
    events = history()
    before = [event.model_dump_json() for event in events]
    store = EventStore(tmp_path / "events.jsonl")
    for event in events:
        store.append(event)
    view = materialize(store.events())
    validate_graph(view)
    assert isomorphic(view, Graph().parse(FIXTURES / "expected.ttl"))
    snapshot = tmp_path / "view.jsonld"
    snapshot.write_text(export_jsonld(view))
    snapshot.unlink()
    del view
    rebuilt = materialize(store.events())
    assert isomorphic(rebuilt, Graph().parse(FIXTURES / "expected.ttl"))
    assert [event.model_dump_json() for event in events] == before
    assert store.export_jsonl() == "".join(line + "\n" for line in before)


def test_jsonld_round_trip_and_provenance():
    graph = materialize(history())
    exported = export_jsonld(graph)
    assert json.loads(exported)["@context"]["@version"] == 1.1
    restored = import_jsonld(exported)
    assert isomorphic(graph, restored)
    for event in history():
        obj = identifier("object", event.id)
        origin = identifier("event", event.id)
        assert (obj, PROV.wasDerivedFrom, origin) in restored
        assert (origin, LH.eventId, Literal(event.id)) in restored
        assert (obj, PROV.wasGeneratedBy, identifier("activity", event.id)) in restored
    artifact = identifier("artifact", "sha256:synthetic/%α")
    assert (artifact, LH.artifactFingerprint, Literal("sha256:synthetic/%α")) in restored
    assert "DO NOT PROJECT" not in exported


def test_sparql_support_negation_conflict_and_retraction():
    graph = materialize(history())
    query = """
        PREFIX lh: <urn:long-haul:epistemic:v1:terms:>
        PREFIX prov: <http://www.w3.org/ns/prov#>
        SELECT ?id WHERE {
            ?e a lh:Evidence ; lh:supports ?claim ; prov:wasDerivedFrom ?event .
            ?event lh:eventId ?id .
        }
    """
    assert [str(row.id) for row in graph.query(query)] == ["evidence:1"]
    positive, negative = (identifier("object", key) for key in ("claim:/P %α", "claim:not-P"))
    assert (negative, LH.negates, positive) in graph
    assert (positive, LH.contradicts, negative) in graph
    assert (negative, LH.contradicts, positive) in graph
    assumption = identifier("object", "assumption:1")
    assert (assumption, PROV.wasInvalidatedBy, identifier("activity", "retraction:1")) in graph
    assert (assumption, LH.supports, positive) in graph  # history is retained
    assert (assumption, RDF.type, LH.Assumption) in graph


@pytest.mark.parametrize("damage", ["provenance", "text", "target", "version", "activity", "artifact", "kind", "conflict", "invalidation"])
def test_shacl_rejects_malformed_graph(damage):
    graph = materialize(history())
    claim = identifier("object", "claim:/P %α")
    if damage == "provenance":
        graph.remove((claim, PROV.wasDerivedFrom, None))
    elif damage == "text":
        graph.set((claim, DCTERMS.description, Literal(17)))
    elif damage == "target":
        graph.set((identifier("object", "retraction:1"), LH.target, URIRef("urn:missing")))
    elif damage == "version":
        graph.set((PROJECTION, LH.version, Literal(2)))
    elif damage == "activity":
        graph.remove((identifier("activity", "claim:/P %α"), PROV.used, None))
    elif damage == "kind":
        graph.remove((claim, RDF.type, LH.Claim))
    elif damage == "conflict":
        graph.remove((claim, LH.contradicts, None))
    elif damage == "invalidation":
        graph.remove((None, PROV.wasInvalidatedBy, None))
    else:
        graph.remove((None, LH.artifactFingerprint, None))
        # Artifact nodes must retain exact fingerprints rather than bare entities.
        graph.add((identifier("artifact", "broken"), LH.artifactFingerprint, Literal("")))
    with pytest.raises(InvalidProjection):
        validate_graph(graph)
    with pytest.raises(InvalidProjection):
        import_jsonld(graph.serialize(format="json-ld"))


@pytest.mark.parametrize("annotation", [
    {"version": True, "kind": "claim", "text": "P"},
    {"version": 1.0, "kind": "claim", "text": "P"},
    {"version": 2, "kind": "claim", "text": "P"},
    {"version": 1, "kind": "claim", "text": "P", "scratchpad": "private"},
    {"version": 1, "kind": "evidence", "text": "E", "supports": ["missing"]},
    {"version": 1, "kind": "claim", "text": "P", "negates": "missing"},
    {"version": 1, "kind": "contradiction", "targets": ["missing", "missing"]},
    {"version": 1, "kind": "retraction", "targets": ["missing"]},
    {"version": 1, "kind": "assumption"},
    {"version": 1, "kind": "claim", "text": "P", "artifacts": [""]},
])
def test_marked_input_fails_closed(annotation):
    event = Event(event_type="inference", payload={"epistemic": annotation})
    with pytest.raises(ValueError):
        materialize([event])


def test_references_require_append_order_and_exact_unique_ids():
    events = history()
    with pytest.raises(ValueError):
        materialize(reversed(events))
    with pytest.raises(ValueError):
        materialize([events[0], events[0]])
    assert identifier("event", "a/b") != identifier("event", "a%2Fb")
    assert identifier("event", " P ") != identifier("event", "P")
    with pytest.raises(ValueError):
        materialize([events[0].model_copy(update={"timestamp": datetime(2026, 1, 1, tzinfo=None)})])  # noqa: DTZ001 — malformed-input fixture
    with pytest.raises(ValueError):
        materialize([events[0].model_copy(update={"schema_version": 2})])


def test_unannotated_history_and_empty_rebuild():
    graph = materialize([Event(event_type="inference", payload={"text": "not opted in", "scratchpad": "private"})])
    assert len(graph) == 2
    validate_graph(graph)
    assert isomorphic(graph, import_jsonld(export_jsonld(graph)))


def test_repeated_retraction_preserves_first_invalidation_and_both_events():
    events = history()
    repeat = events[-1].model_copy(update={"id": "retraction:2"})
    graph = materialize([*events, repeat])
    validate_graph(graph)
    assumption = identifier("object", "assumption:1")
    assert len(list(graph.objects(assumption, PROV.invalidatedAtTime))) == 1
    assert len(list(graph.objects(assumption, PROV.wasInvalidatedBy))) == 2


def test_import_rejects_remote_context_without_fetching():
    with pytest.raises(JsonLdError, match="remote JSON-LD documents are not allowed"):
        import_jsonld('{"@context": "https://example.invalid/context"}')


def test_import_rejects_named_dataset():
    document = json.loads(export_jsonld(materialize(history())))
    document = {"@id": "urn:named", "@graph": document["@graph"]}
    with pytest.raises(ParserError, match="Invalid line"):
        import_jsonld(json.dumps(document))


@pytest.mark.parametrize("annotation", [
    {"version": 1, "kind": "claim", "text": "Q", "negates": "evidence:1"},
    {"version": 1, "kind": "contradiction", "targets": ["claim:not-P", "evidence:1"]},
    {"version": 1, "kind": "retraction", "targets": ["conflict:1"]},
])
def test_reference_kinds_are_checked(annotation):
    event = Event(event_type="decision", payload={"epistemic": annotation})
    with pytest.raises(ValueError):
        materialize([*history(), event])
