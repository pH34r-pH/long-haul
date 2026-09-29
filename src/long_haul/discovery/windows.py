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

    def discover(self) -> Vessel:
        prefix = self.resource_prefix
        resources: list[Resource] = []
        links: list[Link] = []

        processors = _records(
            self._powershell_json(
                "Get-CimInstance Win32_Processor | "
                "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors"
            )
        )
        cpu_id = f"{prefix}-C0"
        cpu_model = processors[0].get("Name") if processors else None
        cpu_metadata: dict[str, Any] = {"provenance": "observed"}
        if processors:
            cpu_metadata.update(
                {
                    "cores": processors[0].get("NumberOfCores"),
                    "logical_processors": processors[0].get("NumberOfLogicalProcessors"),
                }
            )
        resources.append(
            Resource(
                id=cpu_id,
                kind=ResourceKind.CPU,
                model=str(cpu_model).strip() if cpu_model else None,
                architecture="x86_64",
                metadata=cpu_metadata,
            )
        )

        dimms = _records(
            self._powershell_json(
                "Get-CimInstance Win32_PhysicalMemory | "
                "Select-Object DeviceLocator,BankLabel,Capacity,ConfiguredClockSpeed"
            )
        )
        if dimms:
            total_bytes = sum(int(item.get("Capacity") or 0) for item in dimms)
            memory_id = f"{prefix}-M0"
            resources.append(
                Resource(
                    id=memory_id,
                    kind=ResourceKind.MEMORY,
                    model="system RAM",
                    capacities=[Capacity(kind="memory", amount=total_bytes, unit="bytes")],
                    metadata={
                        "provenance": "observed",
                        "modules": [
                            {
                                "device_locator": item.get("DeviceLocator"),
                                "bank_label": item.get("BankLabel"),
                                "capacity_bytes": int(item.get("Capacity") or 0),
                                "configured_clock_mhz": item.get("ConfiguredClockSpeed"),
                            }
                            for item in dimms
                        ],
                    },
                )
            )
        else:
            memory_id = None

        disks = _records(
            self._powershell_json(
                "Get-CimInstance Win32_DiskDrive | "
                "Select-Object Model,FirmwareRevision,Size,InterfaceType"
            )
        )
        for index, disk in enumerate(disks):
            storage_id = f"{prefix}-S{index}"
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

        smi = self._run(
            "nvidia-smi",
            "--query-gpu=index,name,memory.total,pci.bus_id,driver_version",
            "--format=csv,noheader,nounits",
        )
        if smi:
            for line in smi.splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) < 5:
                    continue
                try:
                    index = int(fields[0])
                    memory_mib = float(fields[2])
                except ValueError:
                    continue
                gpu_id = f"{prefix}-G{index}"
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

        return Vessel(
            id=self.vessel_id,
            name=self.name,
            class_name=self.class_name,
            resources=resources,
            links=links,
        )
