"""Best-effort generic Linux probes. Unknown capability is explicit, never inferred."""
from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass

from ..models import Resource, ResourceKind, Vessel
from .parsers import parse_meminfo, parse_nvidia_smi_csv


@dataclass
class Capability:
    name: str
    supported: bool
    detail: str | None = None

class GenericLinuxDiscovery:
    def __init__(self, vessel_id: str, name: str, class_name: str = "station"):
        self.vessel_id, self.name, self.class_name = vessel_id, name, class_name
    def _run(self, *command: str) -> str | None:
        try: return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError): return None
    def capabilities(self) -> list[Capability]:
        return [Capability("proc_meminfo", True), Capability("nvidia_smi", self._run("nvidia-smi", "--help") is not None), Capability("tailscale", self._run("tailscale", "version") is not None)]
    def discover(self) -> Vessel:
        resources=[Resource(id=f"{self.vessel_id}-CPU",kind=ResourceKind.CPU,model=platform.processor() or platform.machine())]
        meminfo=self._run("cat","/proc/meminfo")
        if meminfo: resources.append(parse_meminfo(meminfo,f"{self.vessel_id}-MEM"))
        smi=self._run("nvidia-smi","--query-gpu=name,memory.total","--format=csv,noheader,nounits")
        if smi: resources.extend(parse_nvidia_smi_csv(smi,f"{self.vessel_id}-GPU"))
        return Vessel(id=self.vessel_id,name=self.name,class_name=self.class_name,resources=resources)
