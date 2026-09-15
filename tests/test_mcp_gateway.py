from long_haul.mcp import CapabilityGateway


class FakeReferenceAdapter:
    def vessel_status(self): return {"power": "running"}
    def execution_start(self): return {"state": "started"}
    def execution_status(self): return {"state": "running"}
    def execution_log(self): return {"tail": "hello"}
    def execution_report(self): return {"layers": {"L0": "PASS"}}
    def execution_stop(self): return {"state": "stopped"}


def test_capability_catalog_is_explicit_and_semantic():
    gateway = CapabilityGateway(FakeReferenceAdapter())
    assert gateway.capabilities == (
        "vessel.status",
        "execution.start",
        "execution.status",
        "execution.log",
        "execution.report",
        "execution.stop",
    )
    assert all(item["discoverable"] for item in gateway.inspect())


def test_gateway_dispatches_without_generic_shell():
    gateway = CapabilityGateway(FakeReferenceAdapter())
    result = gateway.invoke("execution.status")
    assert result.status == "OK"
    assert result.data == {"state": "running"}
    assert gateway.invoke("shell.run").status == "UNSUPPORTED"
