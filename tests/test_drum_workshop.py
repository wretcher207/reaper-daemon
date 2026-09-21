"""Composition boundaries and similarity failures from the cymbal revisions."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import drum_workshop as workshop


def brief(**changes):
    return {'request_id': 'song-a-v1', 'project_id': 'song-a',
            'description': 'Heavy groove with space for riffs',
            'tempo': 182, 'bars': 4, 'map': 'RS Monarch',
            'exclude_families': ['ride', 'choke'], **changes}


def dsl(kick='X..x..x.X..x..x.', snare='........X.......', cymbal='crash_l', bars=4, tempo=182):
    return (f'@tempo {tempo}\n@map RS Monarch\n[Phrase] bars={bars} feel=f\n'
            f'grid 16\nkick | {kick} |\nsnare | {snare} |\n'
            f'{cymbal} | X.......x....... |\n')


def populate(path, names=None):
    names = names or ['fresh', 'contrast', 'wildcard']
    for i, name in enumerate(names):
        (path / name / 'candidate.dsl').write_text(dsl(cymbal=['crash_l', 'crash_r', 'china'][i]))
        (path / name / 'intent.json').write_text(json.dumps({
            'premise': 'A test phrase', 'development': 'Repeat for comparison',
            'exploration': 'Deliberately unchanged backbone to exercise the warning'}))


def test_reference_blind_requests_and_context_scopes(tmp_path):
    reference = tmp_path / 'reference.dsl'
    reference.write_text(dsl())
    out = tmp_path / 'workshop'
    preferences = [
        {'scope': 'request', 'target_id': 'other', 'text': 'No hats'},
        {'scope': 'request', 'target_id': 'song-a-v1', 'text': 'Open space'},
        {'scope': 'project', 'target_id': 'other', 'text': 'No china'},
        {'scope': 'global', 'confirmed': True, 'text': 'Phrases should develop'},
        {'scope': 'example', 'text': 'I liked the china in reference one'},
    ]
    workshop.prepare(brief(preferences=preferences, references=[{
        'name': 'Reference secret', 'path': str(reference), 'approved': True}]), out)
    for role in ['fresh', 'wildcard']:
        request = workshop.read_json(out / role / 'request.json')
        assert not request['reference_access']
        assert 'references' not in request and 'preferences' not in request
        assert 'Reference secret' not in json.dumps(request)
    assert workshop.read_json(out / 'reference/request.json')['references'][0]['dsl'] == dsl()
    manifest = workshop.read_json(out / 'workshop.json')
    assert len(manifest['preferences']) == 3
    assert manifest['brief']['exclude_families'] == ['ride', 'choke']
    reference.write_text('changed later')
    assert manifest['references'][0]['dsl'] == dsl()


def test_reference_or_preference_cannot_silently_become_global(tmp_path):
    with pytest.raises(ValueError, match='confirmed'):
        workshop.prepare(brief(preferences=[{'scope': 'global', 'text': 'No ride'}]), tmp_path/'a')
    with pytest.raises(ValueError, match='approved'):
        workshop.prepare(brief(references=[{'name': 'unapproved', 'path': 'a'}]), tmp_path/'b')


def test_repeated_backbone_is_not_hidden_by_kit_cymbals_velocity_or_tempo():
    a = workshop.parse(dsl())
    b = workshop.parse(dsl(cymbal='china', tempo=120).replace('feel=f', 'feel=ff'))
    result = workshop.compare(a, b)
    assert result['backbone_similarity'] == 1
    assert result['orchestration_similarity'] < 1
    assert result['closest_phrase']['similarity'] == 1
    assert workshop.compare(a, workshop.parse(dsl(kick='X...X...X...X...', snare='....X.......X...')))['backbone_similarity'] < .8


def test_moved_reference_phrase_is_found():
    a = workshop.parse(dsl(bars=2))
    b = workshop.parse(dsl(kick='X...X...X...X...', snare='....X.......X...', bars=2) +
                       dsl(bars=2).split('[Phrase]', 1)[1].join(['[Copied]', '']))
    result = workshop.compare(a, b)
    assert result['closest_phrase'] == {'similarity': 1, 'a_bar': 1, 'b_bar': 3, 'bars': 2}


def test_evaluate_keeps_wildcard_even_when_it_matches_and_preserves_dsl(tmp_path):
    workspace = tmp_path / 'workshop'
    workshop.prepare(brief(), workspace)
    populate(workspace)
    source = (workspace / 'wildcard/candidate.dsl').read_bytes()
    result = workshop.evaluate(workspace)
    assert result['ok'], result
    assert result['audition_candidates'] == ['fresh', 'contrast', 'wildcard']
    assert result['candidates'][-1]['protected_wildcard']
    assert all(r['backbone_similarity'] == 1 for r in result['comparisons'])
    assert len(result['review_flags']) == 3
    assert all(c['audition_status'] == 'not_auditioned' for c in result['candidates'])
    assert 'winner' not in result
    assert (workspace / 'wildcard/candidate.dsl').read_bytes() == source
    again = workshop.evaluate(workspace)
    assert result['report'] != again['report']
    assert Path(result['candidates'][0]['midi']).read_bytes() == Path(again['candidates'][0]['midi']).read_bytes()
    # Read the SMF delta times to check that trailing silence reaches the requested length.
    data = Path(result['candidates'][0]['midi']).read_bytes()
    pos, tick = 22, 0
    while pos < len(data):
        delta = 0
        while True:
            byte = data[pos]; pos += 1
            delta = (delta << 7) | (byte & 127)
            if not byte & 128:
                break
        tick += delta
        if data[pos] == 255:
            kind, length = data[pos+1:pos+3]
            pos += 3 + length
            if kind == 47:
                break
        else:
            pos += 3
    assert tick == int.from_bytes(data[12:14], 'big') * 16


@pytest.mark.parametrize('cymbal', ['ride', 'ride_bell', 'china_choke'])
def test_wildcard_cannot_evade_explicit_exclusions(tmp_path, cymbal):
    workspace = tmp_path/'workshop'
    workshop.prepare(brief(), workspace)
    populate(workspace)
    (workspace/'wildcard/candidate.dsl').write_text(dsl(cymbal=cymbal))
    result = workshop.evaluate(workspace)
    assert not result['ok']
    assert result['audition_candidates'] == ['fresh', 'contrast']
    assert 'Excluded families' in result['candidates'][-1]['error']


def test_missing_candidate_and_wrong_duration_are_reported(tmp_path):
    workspace = tmp_path/'workshop'
    workshop.prepare(brief(), workspace)
    populate(workspace)
    (workspace/'fresh/candidate.dsl').unlink()
    (workspace/'wildcard/candidate.dsl').write_text(dsl(bars=5))
    result = workshop.evaluate(workspace)
    assert not result['ok']
    assert result['audition_candidates'] == ['contrast']


def test_feedback_is_scoped_and_bound_to_evaluated_version(tmp_path):
    workspace = tmp_path/'workshop'
    workshop.prepare(brief(), workspace)
    populate(workspace)
    result = workshop.evaluate(workspace)
    (workspace/'fresh/candidate.dsl').write_text('an unrelated later edit')
    record = {'candidate_id': 'fresh', 'report': result['report'],
              'usefulness': 'revise', 'novelty': 'familiar', 'reason': 'Backbone repeats an old part'}
    saved = workshop.feedback(workspace, record)['feedback']
    assert saved['scope'] == 'request'
    assert saved['dsl_sha256'] == result['candidates'][0]['dsl_sha256']
    assert saved['preference_promotion'].startswith('none')
    with pytest.raises(ValueError, match='confirmed'):
        workshop.feedback(workspace, {**record, 'scope': 'global'})
    with pytest.raises(ValueError, match='inside'):
        workshop.feedback(workspace, {**record, 'report': '../foreign.json'})


@pytest.mark.parametrize('changes', [{'bars': 0}, {'bars': 65}, {'tempo': float('nan')},
                                  {'exclude_families': ['unknowable']}, {'unused_knob': True}])
def test_bad_briefs_leave_no_workspace(tmp_path, changes):
    with pytest.raises(ValueError):
        workshop.prepare(brief(**changes), tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_existing_workspace_is_not_overwritten(tmp_path):
    out = tmp_path/'out'
    workshop.prepare(brief(), out)
    with pytest.raises(FileExistsError):
        workshop.prepare(brief(), out)


def test_cli_and_mcp_use_same_offline_workshop(tmp_path, monkeypatch):
    import reaper_mcp
    monkeypatch.setattr(reaper_mcp, '_send', lambda *a, **k: pytest.fail('No bridge calls allowed'))
    path = tmp_path/'brief.json'
    path.write_text(json.dumps(brief()))
    response = reaper_mcp.tool_drum_workshop({'action': 'prepare', 'path': str(path),
                                            'output': str(tmp_path/'mcp')})
    assert not response.get('isError')
    assert json.loads(response['content'][0]['text'])['ok']
    command = subprocess.run([sys.executable, str(workshop.ROOT/'reaperd.py'), 'drum-workshop',
                              'prepare', str(path), '--output', str(tmp_path/'cli')],
                             capture_output=True, text=True)
    assert command.returncode == 0, command.stderr
    assert workshop.read_json(tmp_path/'cli/workshop.json') == workshop.read_json(tmp_path/'mcp/workshop.json')
    assert 'drum_workshop' in reaper_mcp._TOOL_BY_NAME


def test_invalid_end_tick_refused():
    with pytest.raises(ValueError, match='end_tick'):
        workshop.smf.write_smf([{'tick': 0, 'pitch': 36, 'vel': 100, 'dur': 48}], end_tick=10)


def test_malformed_grid_and_intent_do_not_crash_evaluation(tmp_path):
    workspace = tmp_path/'workshop'
    workshop.prepare(brief(), workspace)
    populate(workspace)
    (workspace/'fresh/candidate.dsl').write_text(dsl().replace('grid 16', 'grid'))
    (workspace/'wildcard/intent.json').write_text('[]')
    report = workshop.evaluate(workspace)
    assert report['audition_candidates'] == ['contrast']


def test_aliases_cannot_create_duplicate_physical_hits(tmp_path):
    workspace = tmp_path/'workshop'
    workshop.prepare(brief(), workspace)
    populate(workspace)
    (workspace/'fresh/candidate.dsl').write_text(dsl(cymbal='crash') + 'crash_r | X.......x....... |\n')
    report = workshop.evaluate(workspace)
    assert 'Duplicate' in report['candidates'][0]['error']
