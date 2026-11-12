"""Verify the preliminary shape rejects a dangling policy reference."""
import json
from pathlib import Path

import pytest

from long_haul.resource_policy import ResourceIdentity, ResourcePolicyProfile

validate = pytest.importorskip("pyshacl", reason="install policy test requirements").validate
rdflib = pytest.importorskip("rdflib", reason="install policy test requirements")
ODRL = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")
SHAPES = Path(__file__).resolve().parents[1] / "contracts/resource-policy/profile.shacl.ttl"


def test_policy_reference_must_resolve_to_typed_policy():
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:asset", resource_type="dataset"),
        policy_id="urn:fixture:policy", policy_assigner="urn:fixture:assigner",
    )
    graph = rdflib.Graph().parse(data=json.dumps(profile.to_jsonld()), format="json-ld")
    shapes = rdflib.Graph().parse(SHAPES)
    assert not validate(graph, shacl_graph=shapes)[0]

    graph.add((rdflib.URIRef(profile.policy_id), rdflib.RDF.type, ODRL.Policy))
    assert validate(graph, shacl_graph=shapes)[0]
