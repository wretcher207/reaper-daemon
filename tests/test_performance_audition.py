import pytest
from performance_audition import payload


def test_expression_repeats_same_note_and_resets_controllers():
    p = payload('{test}')
    notes = [e for e in p['events'] if e['type'] == 'note']
    assert notes[0]['pitch'] == notes[1]['pitch']
    assert notes[0]['velocity'] == notes[1]['velocity']
    for controller, expected in ((1, 0), (64, 0), (11, 127)):
        values = [e['value'] for e in p['events'] if e.get('controller') == controller]
        assert values[-1] == expected
    assert all(e['time'] + e.get('duration', 0) <= p['length_seconds'] for e in p['events'])


def test_range_rejected_before_send():
    with pytest.raises(ValueError):
        payload('{test}', pitch=127)
