from long_haul.mcp.identity import AuthenticatedPrincipal


def test_authenticated_principal_public_view_excludes_raw_claims() -> None:
    principal = AuthenticatedPrincipal(
        issuer="https://login.microsoftonline.com/example/v2.0",
        subject="subject-1",
        tenant_id="tenant-1",
        name="Operator",
        client_id="client-1",
        claims={"secretish-provider-detail": "not-for-tool-output"},
    )

    assert principal.public_view() == {
        "issuer": "https://login.microsoftonline.com/example/v2.0",
        "subject": "subject-1",
        "tenant_id": "tenant-1",
        "name": "Operator",
        "client_id": "client-1",
    }
