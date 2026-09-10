"""Pure parsers are unit-testable with captured command output."""
from __future__ import annotations

import re

from ..models import Capacity, Resource, ResourceKind


def parse_nvidia_smi_csv(text: str, prefix: str = "GPU") -> list[Resource]:
    resources = []
    for index, line in enumerate(text.splitlines()):
        bits = [b.strip() for b in line.split(",")]
        if len(bits) >= 2 and bits[1].split()[0].isdigit():
            resources.append(Resource(id=f"{prefix}-{index}", kind=ResourceKind.GPU, model=bits[0], capacities=[Capacity(kind="memory", amount=float(bits[1].split()[0]), unit="MiB")], metadata={"discovery": "probe"}))
    return resources
def parse_meminfo(text: str, resource_id: str) -> Resource:
    match = re.search(r"MemTotal:\s+(\d+) kB", text)
    if not match: raise ValueError("MemTotal not found")
    return Resource(id=resource_id, kind=ResourceKind.MEMORY, model="system RAM", capacities=[Capacity(kind="memory", amount=int(match.group(1))/1024, unit="MiB")])
