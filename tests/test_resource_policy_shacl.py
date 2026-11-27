"""Check the legacy export and canonical compact record shapes."""

import json
from pathlib import Path

import pytest

from long_haul.resource_policy import (
    PolicyRule,
    ResourceIdentity,
    ResourcePolicyProfile,
)
from long_haul.resource_policy_interchange import export_policy_jsonld

validate = pytest.importorskip(
    "pyshacl", reason="install policy test requirements"
).validate
rdflib = pytest.importorskip("rdflib", reason="install policy test requirements")
ODRL = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")
LH = rdflib.Namespace("https://ph34r.dev/ns/long-haul/")
SHAPES = (
    Path(__file__).resolve().parents[1] / "contracts/resource-policy/profile.shacl.ttl"
)


def test_policy_reference_must_resolve_to_typed_policy():
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id="urn:fixture:asset", resource_type="dataset"
        ),
        policy_id="urn:fixture:policy",
        policy_assigner="urn:fixture:assigner",
    )
    graph = rdflib.Graph().parse(data=json.dumps(profile.to_jsonld()), format="json-ld")
    shapes = rdflib.Graph().parse(SHAPES)
    assert not validate(graph, shacl_graph=shapes)[0]

    graph.add((rdflib.URIRef(profile.policy_id), rdflib.RDF.type, ODRL.Policy))
    assert validate(graph, shacl_graph=shapes)[0]


def test_compact_record_shape_and_ordered_rule_graph():
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id="urn:fixture:asset", resource_type="dataset"
        ),
        policy_id="urn:fixture:policy",
        policy_assigner="urn:fixture:assigner",
        permissions=[
            PolicyRule(
                rule_id="scoped", actions=["search", "index"], crew_ids=["crew-a"]
            )
        ],
    )
    graph = rdflib.Graph().parse(data=export_policy_jsonld(profile), format="json-ld")
    shapes = rdflib.Graph().parse(SHAPES)
    record = graph.value(predicate=rdflib.RDF.type, object=LH.ResourcePolicyRecord)
    assert record is not None
    assert validate(graph, shacl_graph=shapes)[0]
    (rule,) = graph.items(graph.value(record, LH.permissions))
    assert list(graph.items(graph.value(rule, LH.actions))) == [
        rdflib.Literal("search"),
        rdflib.Literal("index"),
    ]
    assert list(graph.items(graph.value(rule, LH.crew_ids))) == [
        rdflib.Literal("crew-a")
    ]
    assert graph.value(record, LH.prohibitions) == rdflib.RDF.nil
    assert not list(graph.triples((None, ODRL.permission, None)))

    graph.remove((record, LH.identity, None))
    assert not validate(graph, shacl_graph=shapes)[0]
