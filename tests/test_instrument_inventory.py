from instrument_inventory import inventory


def test_discovery_does_not_claim_loading(tmp_path):
    (tmp_path / 'reaper-vstplugins64.ini').write_text('Serum.dll=123,456,Serum 2 (Xfer Records)!!!VSTi\n')
    (tmp_path / 'Flute.SerumPreset').write_text('opaque')
    result = inventory(str(tmp_path), [str(tmp_path)], limit=1)
    assert result['plugins'][0]['instrument_flag']
    assert not result['plugins'][0]['verified_loaded']
    assert result['content'][0]['loading'] == 'plugin_specific'
    assert not result['content'][0]['dependencies_verified']


def test_content_bound_and_filter(tmp_path):
    for name in ('Flute A', 'Flute B', 'Pad'):
        (tmp_path / (name + '.RfxChain')).write_text('')
    result = inventory(str(tmp_path), [str(tmp_path)], query='flute', limit=1)
    assert result['truncated']
    assert len(result['content']) == 1
    assert result['content'][0]['loading'] == 'add_fx_chain'
