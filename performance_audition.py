"""Deterministic melodic controller test; insert through insert_midi_events."""


def payload(track_guid, start_seconds=0, pitch=69):
    if not isinstance(pitch, int) or not 0 <= pitch <= 115:
        raise ValueError('pitch must be 0..115')
    if not track_guid:
        raise ValueError('track_guid required')
    events = []
    def cc(time, controller, value):
        events.append(dict(type='cc', time=time, controller=controller, value=value))
    def note(time, key, duration=1):
        events.append(dict(type='note', time=time, duration=duration, pitch=key, velocity=90))
    cc(0, 1, 0)
    cc(0, 64, 0)
    cc(0, 11, 127)
    note(.1, pitch)
    cc(2, 11, 40)
    note(2.1, pitch)
    cc(4, 11, 127)
    cc(4, 1, 127)
    note(4.1, pitch, 2)
    cc(7, 1, 0)
    cc(7, 64, 127)
    note(7.1, pitch)
    cc(9, 64, 0)
    note(10, pitch - 12 if pitch >= 12 else pitch)
    note(12, pitch + 12)
    return dict(target_track_guid=track_guid, start_seconds=start_seconds,
                length_seconds=15, events=events)
