from long_haul.mcp import CapabilityGateway, CapabilityReason, CapabilityStatus
from long_haul.mcp.identity import AuthenticatedPrincipal


class InactiveAdapter:
    def __getattr__(self, name):
        raise AssertionError(f"Unadmitted adapter operation: {name}")


def test_capability_catalog_is_explicit_and_semantic():
    gateway = CapabilityGateway(InactiveAdapter())
    assert gateway.capabilities == (
        "vessel.status",
        "execution.start",
        "execution.status",
        "execution.log",
        "execution.report",
        "execution.stop",
    )
    assert all(item["discoverable"] and not item["authorized"] and not item["executable"]
               for item in gateway.inspect())


def test_gateway_without_authenticated_context_denies_every_discoverable_capability():
    gateway = CapabilityGateway(InactiveAdapter())
    for entry in gateway.inspect():
        result = gateway.invoke(entry["capability"])
        assert result.schema_version == 1
        assert result.status is CapabilityStatus.DENIED
        assert result.reason_code is CapabilityReason.AUTHENTICATED_CONTEXT_REQUIRED
        assert result.data == {}
        assert entry["authorization"] == result.reason_code


def test_caller_constructed_identity_does_not_grant_authority():
    principal = AuthenticatedPrincipal(issuer="fixture", subject="fixture", claims={"role": "operator"})
    gateway = CapabilityGateway(InactiveAdapter())
    catalog = gateway.inspect(principal=principal)
    assert all(not entry["authorized"] and entry["authorization"] == CapabilityReason.POLICY_UNAVAILABLE
               for entry in catalog)
    assert gateway.invoke("execution.start", principal=principal).reason_code is CapabilityReason.POLICY_UNAVAILABLE
    result = gateway.invoke("shell.run", principal=principal)
    assert result.status is CapabilityStatus.UNSUPPORTED
    assert result.reason_code is CapabilityReason.UNSUPPORTED
