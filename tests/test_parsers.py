from long_haul.discovery.parsers import parse_meminfo, parse_nvidia_smi_csv


def test_nvidia_csv_fixture_parser():
    values=parse_nvidia_smi_csv("NVIDIA GeForce GTX 1070, 8119\nNVIDIA GeForce GTX 1070, 8119\n","ANC-G")
    assert [v.id for v in values] == ["ANC-G-0","ANC-G-1"]
    assert values[0].capacity_mib() == 8119

def test_meminfo_fixture_parser():
    assert parse_meminfo("MemTotal:       24576000 kB\n","ANC-M0").capacity_mib() == 24000
