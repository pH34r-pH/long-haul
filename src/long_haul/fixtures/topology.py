"""Conspicuously simulated, non-empirical fleet data for development only."""
from ..models import Capacity, Link, Resource, ResourceKind, Vessel


def anchorage() -> Vessel:
    return Vessel(id="anchorage-fixture", name="Anchorage (SIMULATED)", class_name="station", resources=[
        Resource(id="ANC-C0", kind=ResourceKind.CPU, model="Ryzen 7700-class", metadata={"provenance":"simulated"}),
        Resource(id="ANC-M0", kind=ResourceKind.MEMORY, capacities=[Capacity(kind="memory", amount=24576, unit="MiB")], metadata={"provenance":"simulated"}),
        Resource(id="ANC-G0", kind=ResourceKind.GPU, model="GTX 1070", capacities=[Capacity(kind="memory",amount=8192,unit="MiB")], metadata={"provenance":"simulated","path":"higher"}),
        Resource(id="ANC-G1", kind=ResourceKind.GPU, model="GTX 1070", capacities=[Capacity(kind="memory",amount=8192,unit="MiB")], metadata={"provenance":"simulated","path":"lower"}),
        Resource(id="ANC-S0", kind=ResourceKind.STORAGE, model="NVMe", capacities=[Capacity(kind="storage",amount=2048,unit="GiB")], metadata={"provenance":"simulated"}),],
        links=[Link(source="ANC-C0",target="ANC-G0",kind="pcie",notes="SIMULATED asymmetric primary path"),Link(source="ANC-C0",target="ANC-G1",kind="pcie",notes="SIMULATED asymmetric secondary path"),Link(source="ANC-S0",target="ANC-M0",kind="storage_bus",notes="SIMULATED")])
def kestrel() -> Vessel:
    return Vessel(id="kestrel-fixture",name="Kestrel (SIMULATED)",class_name="ship",resources=[Resource(id="KST-C0",kind=ResourceKind.CPU,model="Orin Nano CPU",metadata={"provenance":"simulated"}),Resource(id="KST-M0",kind=ResourceKind.MEMORY,capacities=[Capacity(kind="memory",amount=8192,unit="MiB")],metadata={"provenance":"simulated","unified":True}),Resource(id="KST-G0",kind=ResourceKind.GPU,model="Orin GPU",capacities=[Capacity(kind="memory",amount=8192,unit="MiB")],metadata={"provenance":"simulated","unified":True}),Resource(id="KST-S0",kind=ResourceKind.STORAGE,capacities=[Capacity(kind="storage",amount=128,unit="GiB")],metadata={"provenance":"simulated"})],links=[Link(source="KST-C0",target="KST-G0",kind="memory",notes="SIMULATED unified memory")])
def tailscale(path: str) -> Link:
    return Link(source="anchorage-fixture",target="kestrel-fixture",kind="tailscale",direct=path=="direct",path=path,measured=False,notes="SIMULATED—NOT A MEASUREMENT")
