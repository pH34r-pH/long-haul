"""Adapters return normalized objects; command execution remains at the edge."""
from __future__ import annotations

from typing import Protocol

from ..models import Vessel


class FleetDiscoveryAdapter(Protocol):
    def discover(self) -> Vessel: ...
class TopologyProbe(Protocol):
    def probe(self) -> Vessel: ...
class NetworkProbe(Protocol):
    def probe_network(self, peer: str) -> list[dict[str, object]]: ...
class StorageProbe(Protocol):
    def probe_storage(self) -> list[dict[str, object]]: ...
class BenchmarkRunner(Protocol):
    def run(self, request: object) -> object: ...
class RuntimeAdapter(Protocol):
    runtime_id: str
    def capabilities(self) -> dict[str, object]: ...
