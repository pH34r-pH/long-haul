from long_haul.reference import run
from long_haul.validation import LayerState, ReferenceReport


def test_l3_unknown_retains_lower_layers(tmp_path):
    report=run("/missing/llama-cli","/missing/model.gguf",str(tmp_path))
    assert [x.state for x in report.layers] == [LayerState.PASS,LayerState.PASS,LayerState.PASS,LayerState.UNKNOWN]


def test_checkpoint_layer_can_be_replaced_without_losing_prior_evidence():
    report = ReferenceReport()
    report.record(0, LayerState.PASS, "healthy")
    report.record(5, LayerState.UNKNOWN, "in progress")
    report.set_layer(5, LayerState.FAIL_RUNTIME, "timed out")
    assert [(item.layer, item.state) for item in report.layers] == [(0, LayerState.PASS), (5, LayerState.FAIL_RUNTIME)]
