"""Best-effort Windows inventory for physical workstation vessels."""
from __future__ import annotations

import json
import subprocess
from typing import Any

from ..models import Capacity, Link, Resource, ResourceKind, Vessel


def _records(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


class GenericWindowsDiscovery:
    """Discover local Windows resources without inferring unmeasured performance."""

    def __init__(
        self,
        vessel_id: str,
        name: str,
        class_name: str = "station",
        resource_prefix: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        self.vessel_id = vessel_id
        self.name = name
        self.class_name = class_name
        self.resource_prefix = resource_prefix or vessel_id[:3].upper()
        self.timeout_seconds = timeout_seconds

    def _run(self, *command: str) -> str | None:
        try:
            return subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None

    def _powershell_json(self, expression: str) -> object:
        output = self._run(
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            expression + " | ConvertTo-Json -Compress -Depth 6",
        )
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return None

    def _cpu(self) -> Resource:
        processors = _records(
            self._powershell_json(
                "Get-CimInstance Win32_Processor | "
                "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors"
            )
        )
        observed = processors[0] if processors else {}
        metadata: dict[str, Any] = {"provenance": "observed"}
        if observed:
            metadata.update(
                {
                    "cores": observed.get("NumberOfCores"),
                    "logical_processors": observed.get("NumberOfLogicalProcessors"),
                }
            )
        model = str(observed.get("Name") or "").strip() or None
        return Resource(
            id=f"{self.resource_prefix}-C0",
            kind=ResourceKind.CPU,
            model=model,
            architecture="x86_64",
            metadata=metadata,
        )

    def _memory(self) -> Resource | None:
        dimms = _records(
            self._powershell_json(
                "Get-CimInstance Win32_PhysicalMemory | "
                "Select-Object DeviceLocator,BankLabel,Capacity,ConfiguredClockSpeed"
            )
        )
        if not dimms:
            return None
        total_bytes = sum(int(item.get("Capacity") or 0) for item in dimms)
        modules = [
            {
                "device_locator": item.get("DeviceLocator"),
                "bank_label": item.get("BankLabel"),
                "capacity_bytes": int(item.get("Capacity") or 0),
                "configured_clock_mhz": item.get("ConfiguredClockSpeed"),
            }
            for item in dimms
        ]
        return Resource(
            id=f"{self.resource_prefix}-M0",
            kind=ResourceKind.MEMORY,
            model="system RAM",
            capacities=[Capacity(kind="memory", amount=total_bytes, unit="bytes")],
            metadata={"provenance": "observed", "modules": modules},
        )

    def _storage(self, memory_id: str | None) -> tuple[list[Resource], list[Link]]:
        disks = _records(
            self._powershell_json(
                "Get-CimInstance Win32_DiskDrive | "
                "Select-Object Model,FirmwareRevision,Size,InterfaceType"
            )
        )
        resources: list[Resource] = []
        links: list[Link] = []
        for index, disk in enumerate(disks):
            storage_id = f"{self.resource_prefix}-S{index}"
            size = int(disk.get("Size") or 0)
            capacities = [Capacity(kind="storage", amount=size, unit="bytes")] if size else []
            resources.append(
                Resource(
                    id=storage_id,
                    kind=ResourceKind.STORAGE,
                    model=str(disk.get("Model") or "").strip() or None,
                    capacities=capacities,
                    metadata={
                        "provenance": "observed",
                        "firmware_revision": disk.get("FirmwareRevision"),
                        "interface_type": disk.get("InterfaceType"),
                    },
                )
            )
            if memory_id:
                links.append(
                    Link(
                        source=storage_id,
                        target=memory_id,
                        kind="storage_bus",
                        measured=False,
                        notes="Observed local storage; practical throughput remains unmeasured.",
                    )
                )
        return resources, links

    def _gpus(self, cpu_id: str) -> tuple[list[Resource], list[Link]]:
        smi = self._run(
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,pci.bus_id,driver_version",
            "--format=csv,noheader,nounits",
        )
        resources: list[Resource] = []
        links: list[Link] = []
        if not smi:
            return resources, links
        for line in smi.splitlines():
            fields = [field.strip() for field in line.split(",")]
            if len(fields) < 5:
                continue
            try:
                index = int(fields[0])
                memory_mib = float(fields[2])
            except ValueError:
                continue
            gpu_id = f"{self.resource_prefix}-G{index}"
            resources.append(
                Resource(
                    id=gpu_id,
                    kind=ResourceKind.GPU,
                    model=fields[1],
                    capacities=[Capacity(kind="memory", amount=memory_mib, unit="MiB")],
                    metadata={
                        "provenance": "observed",
                        "nvidia_index": index,
                        "pci_bus_id": fields[3],
                        "driver_version": fields[4],
                    },
                )
            )
            links.append(
                Link(
                    source=cpu_id,
                    target=gpu_id,
                    kind="pcie",
                    path=fields[3],
                    measured=True,
                    notes=(
                        "PCI bus identity observed from nvidia-smi; "
                        "link width and throughput remain unmeasured."
                    ),
                )
            )
        return resources, links

    def discover(self) -> Vessel:
        cpu = self._cpu()
        memory = self._memory()
        storage, storage_links = self._storage(memory.id if memory else None)
        gpus, gpu_links = self._gpus(cpu.id)
        resources = [cpu, *([memory] if memory else []), *storage, *gpus]
        return Vessel(
            id=self.vessel_id,
            name=self.name,
            class_name=self.class_name,
            resources=resources,
            links=[*storage_links, *gpu_links],
        )
