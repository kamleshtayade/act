import pytest

from mcx_agent.signals import SignalParseError, lipi_delta_imbalance_rule, parse_gocharting_alert


def test_parse_alertcondition_message():
    s = parse_gocharting_alert(b"LONG|delta_imbalance|crudeoil")
    assert s.side == "LONG" and s.symbol == "CRUDEOIL" and s.delta is None


def test_parse_inline_alert_with_values():
    s = parse_gocharting_alert("SHORT|delta=-41000|cvd=-120000.5")
    assert s.side == "SHORT" and s.delta == -41000 and s.cvd == -120000.5


def test_parse_json_wrapped_message():
    s = parse_gocharting_alert('{"message": "LONG|delta=45000|cvd=9"}')
    assert s.side == "LONG" and s.delta == 45000


def test_parse_json_fields():
    s = parse_gocharting_alert('{"side": "short", "delta": -50000}')
    assert s.side == "SHORT" and s.delta == -50000


@pytest.mark.parametrize("body", ["", "BUY|x", '{"foo": 1}', "LONG|delta=abc"])
def test_parse_rejects_garbage(body):
    with pytest.raises(SignalParseError):
        parse_gocharting_alert(body)


def test_lipi_rule_matches_script():
    cvds = [0.0] * 20 + [100.0]
    assert lipi_delta_imbalance_rule([0] * 20 + [40_000], cvds, 20) == "LONG"
    assert lipi_delta_imbalance_rule([0] * 20 + [39_999], cvds, 20) is None
    # bearish delta while CVD is rising -> no short
    assert lipi_delta_imbalance_rule([0] * 20 + [-40_000], cvds, 20) is None
    falling = [100.0] * 20 + [0.0]
    assert lipi_delta_imbalance_rule([0] * 20 + [-40_000], falling, 20) == "SHORT"
    # not enough history for cvd[20]
    assert lipi_delta_imbalance_rule([40_000] * 5, [1, 2, 3, 4, 5], 4) is None
