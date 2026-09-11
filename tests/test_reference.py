from long_haul.reference import run
from long_haul.validation import LayerState


def test_l3_unknown_retains_lower_layers(tmp_path):
    report=run("/missing/llama-cli","/missing/model.gguf",str(tmp_path))
    assert [x.state for x in report.layers] == [LayerState.PASS,LayerState.PASS,LayerState.PASS,LayerState.UNKNOWN]
