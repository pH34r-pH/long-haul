from long_haul.discovery.windows import GenericWindowsDiscovery


class FixtureWindowsDiscovery(GenericWindowsDiscovery):
    def _powershell_json(self, expression: str) -> object:
        if "Win32_Processor" in expression:
            return {
                "Name": "AMD Ryzen 9 7900X 12-Core Processor",
                "NumberOfCores": 12,
                "NumberOfLogicalProcessors": 24,
            }
        if "Win32_PhysicalMemory" in expression:
            return {
                "DeviceLocator": "DIMMB2",
                "BankLabel": "P0 CHANNEL B",
                "Capacity": 25769803776,
                "ConfiguredClockSpeed": 5200,
            }
        if "Win32_DiskDrive" in expression:
            return [
                {
                    "Model": "Samsung SSD 980 PRO 2TB",
                    "FirmwareRevision": "5B2QGXA7",
                    "Size": 2000396321280,
                    "InterfaceType": "SCSI",
                },
                {
                    "Model": "Samsung SSD 850 EVO 500GB",
                    "FirmwareRevision": "EMT02B6Q",
                    "Size": 500105249280,
                    "InterfaceType": "IDE",
                },
            ]
        raise AssertionError(expression)

    def _run(self, *command: str) -> str | None:
        assert command[0] == "nvidia-smi"
        return (
            "0, NVIDIA GeForce GTX 1070, 8192, 00000000:01:00.0, 580.88\n"
            "1, NVIDIA GeForce GTX 1070, 8192, 00000000:06:00.0, 580.88\n"
        )


def test_windows_discovery_preserves_observed_anchorage_asymmetry():
    vessel = FixtureWindowsDiscovery(
        "anchorage",
        "Anchorage",
        resource_prefix="ANC",
    ).discover()

    resources = {resource.id: resource for resource in vessel.resources}
    assert resources["ANC-C0"].model == "AMD Ryzen 9 7900X 12-Core Processor"
    assert resources["ANC-M0"].capacity_mib() == 24576
    assert resources["ANC-G0"].metadata["pci_bus_id"] == "00000000:01:00.0"
    assert resources["ANC-G1"].metadata["pci_bus_id"] == "00000000:06:00.0"
    assert resources["ANC-S0"].model == "Samsung SSD 980 PRO 2TB"
    assert resources["ANC-S1"].model == "Samsung SSD 850 EVO 500GB"

    gpu_links = [link for link in vessel.links if link.kind == "pcie"]
    assert {link.target for link in gpu_links} == {"ANC-G0", "ANC-G1"}
    assert {link.path for link in gpu_links} == {
        "00000000:01:00.0",
        "00000000:06:00.0",
    }
    assert all(link.measured for link in gpu_links)
