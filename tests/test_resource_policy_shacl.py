"""Verify the preliminary shape rejects a dangling policy reference."""
import json
from pathlib import Path

from pyshacl import validate
from rdflib import RDF, Graph, Namespace, URIRef

from long_haul.resource_policy import ResourceIdentity, ResourcePolicyProfile

ODRL = Namespace("http://www.w3.org/ns/odrl/2/")
SHAPES = Path(__file__).resolve().parents[1] / "contracts/resource-policy/profile.shacl.ttl"


def test_policy_reference_must_resolve_to_typed_policy():
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:asset", resource_type="dataset"),
        policy_id="urn:fixture:policy", policy_assigner="urn:fixture:assigner",
    )
    graph = Graph().parse(data=json.dumps(profile.to_jsonld()), format="json-ld")
    shapes = Graph().parse(SHAPES)
    assert not validate(graph, shacl_graph=shapes)[0]

    graph.add((URIRef(profile.policy_id), RDF.type, ODRL.Policy))
    assert validate(graph, shacl_graph=shapes)[0]
