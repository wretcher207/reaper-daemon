"""Offline composition requests, structural comparison and audition feedback.

The caller composes the DSL. This module never invokes a model, judges musical
quality, trains on feedback or contacts REAPER. CLI and MCP share this API.
"""
from collections import Counter
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'skills' / 'drum-apparatus'))
from drumgen import groovekit, smf  # noqa: E402
from drumgen.goldenrule import enforce, violations  # noqa: E402

FAMILIES = {'kick', 'snare', 'tom', 'hat', 'ride', 'crash', 'china',
            'splash', 'stack', 'bell', 'choke'}
GRIDS = {8, 12, 16, 24, 32, 48, 64}


def read_json(path):
    return json.loads(read_text(path))


def read_text(path):
    path = Path(path)
    if path.stat().st_size > 262144:
        raise ValueError('Input exceeds 256 KiB')
    return path.read_text(encoding='utf-8-sig')


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


def text_value(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f'{name} must be nonempty text, at most 4000 characters')
    return value


def family(lane):
    if lane.endswith('_choke'):
        return 'choke'
    return next((f for f in FAMILIES if lane.startswith(f)), lane)


def parse(text):
    # Bound grids before calling the renderer/parser (zero would divide by zero).
    if not isinstance(text, str) or len(text) > 262144:
        raise ValueError('DSL must be text of at most 256 KiB')
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith('grid') and not line.startswith('#'):
            parts = line.split()
            if len(parts) != 2 or int(parts[1]) not in GRIDS:
                raise ValueError('Supported grids: 8, 12, 16, 24, 32, 48, 64')
    parsed = groovekit.parse_dsl(text)
    integer(parsed['tempo'], 'tempo', 40, 320)
    integer(parsed['ppq'], 'ppq', 96, 9600)
    bars = sum(s['bars'] for s in parsed['sections'])
    integer(bars, 'bars', 1, 64)
    return parsed


def active_preferences(preferences, context):
    if not isinstance(preferences, list) or len(preferences) > 128:
        raise ValueError('preferences must be a list of at most 128 records')
    active = []
    for p in preferences:
        if not isinstance(p, dict):
            raise ValueError('Each preference must be an object')
        text_value(p.get('text'), 'preference text')
        scope = p.get('scope')
        if scope not in {'request', 'project', 'global', 'example'}:
            raise ValueError('Preference scope: request, project, global or example')
        if scope in {'request', 'project'}:
            target = text_value(p.get('target_id'), 'preference target_id')
            if target != context[scope + '_id']:
                continue
        if scope == 'global' and p.get('confirmed') is not True:
            raise ValueError('Global preferences require confirmed: true')
        # Examples are observations; never convert them into rules.
        active.append(dict(p))
    return active


def validate_brief(brief):
    if not isinstance(brief, dict):
        raise ValueError('brief must be an object')
    allowed = {'request_id', 'project_id', 'description', 'tempo', 'bars', 'map',
               'seed', 'exclude_families', 'preferences', 'references'}
    if set(brief) - allowed:
        raise ValueError(f'Unknown brief fields: {sorted(set(brief) - allowed)}')
    for key in ('request_id', 'project_id', 'description', 'map'):
        text_value(brief.get(key), key)
    integer(brief.get('tempo'), 'tempo', 40, 320)
    integer(brief.get('bars'), 'bars', 1, 64)
    integer(brief.get('seed', 1), 'seed', 0, 2**31 - 1)
    if brief['map'] not in groovekit.load_maps():
        raise ValueError('Unknown drum map')
    excluded = brief.get('exclude_families', [])
    if not isinstance(excluded, list) or any(f not in FAMILIES for f in excluded):
        raise ValueError('exclude_families contains an unknown drum family')
    active_preferences(brief.get('preferences', []), brief)
    refs = brief.get('references', [])
    if not isinstance(refs, list) or len(refs) > 8:
        raise ValueError('references must be a list of at most 8 approved DSL files')
    return brief


def prepare(brief, output, base=None):
    """Freeze a brief and references. Fresh requests contain no reference data."""
    validate_brief(brief)
    references = []
    for ref in brief.get('references', []):
        if not isinstance(ref, dict) or ref.get('approved') is not True:
            raise ValueError('Each reference requires approved: true')
        name = text_value(ref.get('name'), 'reference name')
        path = Path(text_value(ref.get('path'), 'reference path'))
        if not path.is_absolute():
            path = Path(base or '.') / path
        source = read_text(path)
        parse(source)
        references.append({'name': name, 'dsl': source, 'sha256': digest(source)})
    context = {k: brief[k] for k in ('request_id', 'project_id')}
    public = {k: brief[k] for k in ('description', 'tempo', 'bars', 'map')}
    public['exclude_families'] = brief.get('exclude_families', [])
    roles = ['fresh', 'reference' if references else 'contrast', 'wildcard']
    requests = []
    for role in roles:
        request = {'candidate_id': role, 'brief': public,
                   'reference_access': role == 'reference',
                   'task': ('Compose a coherent phrase with development and a clear landing. '
                            'Choose a rhythmic premise and explain its development in intent.json. '
                            'Use the drum DSL. Do not encode taste as cymbal quotas. '
                            'Respect the brief exclusions; leave other musical choices open.'),
                   'deliverables': ['candidate.dsl', 'intent.json'],
                   'intent_fields': ['premise', 'development', 'exploration']}
        if role == 'reference':
            request['references'] = references
            request['direction'] = 'Transform an idea from these references; explain what you changed.'
        elif role == 'wildcard':
            request['direction'] = ('Explore a coherent idea outside the expected treatment. '
                                    'Novelty may come from rhythm, space or form; extra notes are optional.')
        elif role == 'contrast':
            request['direction'] = 'Explore an independent rhythmic premise; do not vary a sibling candidate.'
        else:
            request['direction'] = 'Compose from this brief alone without reference patterns or sibling candidates.'
        requests.append(request)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = {'version': 1, 'context': context, 'brief': public,
                'seed': brief.get('seed', 1), 'candidates': roles, 'references': references,
                'preferences': active_preferences(brief.get('preferences', []), context)}
    write_json(output / 'workshop.json', manifest)
    for request in requests:
        folder = output / request['candidate_id']
        folder.mkdir()
        write_json(folder / 'request.json', request)
    return {'ok': True, 'workspace': str(output), 'candidates': roles,
            'next': 'Compose each request in a separate context; evaluate after writing the deliverables.',
            'context_isolation': 'Separate files do not sandbox an agent. Supply only its request to each composer.'}


def structure(parsed):
    """Exact score onsets in bar units, independent of kit, velocity and jitter."""
    events = set()
    start = 0
    for section in parsed['sections']:
        grid = section['grid']
        for lane in section['lanes']:
            for step in range(section['bars'] * grid):
                if lane['cells'][step % len(lane['cells'])] != '.':
                    events.add((Fraction(start) + Fraction(step, grid), family(lane['lane'])))
        start += section['bars']
    return events, start


def jaccard(a, b):
    return len(a & b) / len(a | b) if a or b else 0.0


def compare(a, b):
    ae, ab = structure(a)
    be, bb = structure(b)
    backbone = {'kick', 'snare'}
    aa = {(t, f) for t, f in ae if f in backbone}
    ba = {(t, f) for t, f in be if f in backbone}
    window = min(2, ab, bb)
    best = (0.0, 0, 0)
    awindows = [{(t - x, f) for t, f in aa if x <= t < x + window}
                for x in range(ab - window + 1)]
    bwindows = [{(t - y, f) for t, f in ba if y <= t < y + window}
                for y in range(bb - window + 1)]
    for x, aw in enumerate(awindows):
        for y, bw in enumerate(bwindows):
            score = jaccard(aw, bw)
            if score > best[0]:
                best = (score, x + 1, y + 1)
    return {'backbone_similarity': round(jaccard(aa, ba), 4),
            'orchestration_similarity': round(jaccard(ae, be), 4),
            'same_backbone': bool(aa) and aa == ba and ab == bb,
            'closest_phrase': {'similarity': round(best[0], 4),
                               'a_bar': best[1], 'b_bar': best[2], 'bars': window},
            'meaning': 'Shared score onsets; not a probability of copying or a musical quality score.'}


def local_file(workspace, relative):
    path = (workspace / relative).resolve()
    if not path.is_relative_to(workspace):
        raise ValueError('Workshop files must stay inside the workspace')
    return path


def load_workspace(path):
    workspace = Path(path).resolve()
    manifest = read_json(local_file(workspace, 'workshop.json'))
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or manifest.get('candidates') not in (
            ['fresh', 'reference', 'wildcard'], ['fresh', 'contrast', 'wildcard']):
        raise ValueError('Invalid workshop manifest')
    validate_brief({**manifest['context'], **manifest['brief'], 'seed': manifest['seed'],
                    'preferences': manifest['preferences']})
    if not isinstance(manifest['references'], list) or len(manifest['references']) > 8:
        raise ValueError('Invalid workshop references')
    for ref in manifest['references']:
        if not isinstance(ref['dsl'], str) or len(ref['dsl']) > 262144:
            raise ValueError('Invalid reference DSL')
        if digest(ref['dsl']) != ref['sha256']:
            raise ValueError('Reference snapshot changed')
        parse(ref['dsl'])
    return workspace, manifest


def evaluate(path):
    workspace, manifest = load_workspace(path)
    run = workspace / ('evaluation-' + uuid.uuid4().hex)
    run.mkdir()
    results, parsed_candidates = [], {}
    for index, name in enumerate(manifest['candidates']):
        item = {'candidate_id': name, 'protected_wildcard': name == 'wildcard',
                'audition_status': 'not_auditioned', 'warnings': []}
        results.append(item)
        try:
            source = read_text(local_file(workspace, name + '/candidate.dsl'))
            intent = read_json(local_file(workspace, name + '/intent.json'))
            if not isinstance(intent, dict):
                raise ValueError('intent.json must contain an object')
            for key in ('premise', 'development', 'exploration'):
                text_value(intent.get(key), key)
            parsed = parse(source)
            events, bars = structure(parsed)
            brief = manifest['brief']
            if (parsed['tempo'], parsed['map'], bars) != (brief['tempo'], brief['map'], brief['bars']):
                raise ValueError('Candidate tempo, map and bars must match the brief')
            forbidden = {f for _, f in events} & set(brief['exclude_families'])
            if forbidden:
                raise ValueError(f'Excluded families present: {sorted(forbidden)}')
            if not events:
                raise ValueError('Candidate contains no hits')
            # Keep scores unchanged; render through the existing humanizer.
            rendered, info = groovekit.build(source, seed=manifest['seed'] + index)
            end = bars * 4 * info['ppq']
            rendered = sorted(rendered, key=lambda n: (n['tick'], n['pitch']))
            for note in rendered:
                note['tick'] = min(max(0, note['tick']), end - 1)
                note['dur'] = min(note['dur'], end - note['tick'])
            # Same-pitch duplicate triggers are invalid. Shorten only overlapping releases.
            previous = {}
            for note in rendered:
                old = previous.get(note['pitch'])
                if old:
                    if old['tick'] == note['tick']:
                        raise ValueError('Duplicate same-pitch trigger after kit mapping')
                    old['dur'] = min(old['dur'], note['tick'] - old['tick'])
                previous[note['pitch']] = note
            rows = [dict(index=i, ppq=n['tick'], pitch=n['pitch']) for i, n in enumerate(rendered)]
            vel = enforce(rows, {i: n['vel'] for i, n in enumerate(rendered)},
                          min_gap=1,
                          bands={pitch: (min(n['vel'] for n in rendered if n['pitch'] == pitch),
                                         max(n['vel'] for n in rendered if n['pitch'] == pitch))
                                 for pitch in {n['pitch'] for n in rendered}})
            if violations(rows, vel):
                raise ValueError('Cannot enforce dynamics inside rendered velocity bands')
            for i, note in enumerate(rendered):
                note['vel'] = vel[i]
            midi = smf.write_smf(rendered, ppq=info['ppq'], tempo=info['tempo'], end_tick=end)
            readback = smf.parse_smf(midi)
            expected = [{'tick': n['tick'], 'pitch': n['pitch'], 'vel': n['vel']} for n in rendered]
            if readback['notes'] != expected:
                raise ValueError('MIDI serialization did not preserve the notes')
            (run / (name + '.mid')).write_bytes(midi)
            (run / (name + '.dsl')).write_text(source, encoding='utf-8')
            item.update(valid=True, dsl_sha256=digest(source), intent=intent,
                        midi=str(run / (name + '.mid')), notes=len(rendered),
                        length_seconds=bars * 240 / brief['tempo'],
                        length_bars=bars, family_hits=dict(Counter(f for _, f in events)))
            item['warnings'].extend(info['warnings'])
            hands = Counter(t for t, f in events if f not in {'kick'})
            if any(n > 2 for n in hands.values()):
                item['warnings'].append('Review simultaneous hand parts; family counts are only a rough check.')
            parsed_candidates[name] = parsed
        except (ValueError, OSError, KeyError, TypeError, IndexError, ZeroDivisionError) as exc:
            item.update(valid=False, error=str(exc))
    comparisons = []
    for i, a in enumerate(parsed_candidates):
        for b in list(parsed_candidates)[i + 1:]:
            comparisons.append({'a': a, 'b': b, **compare(parsed_candidates[a], parsed_candidates[b])})
    reference_matches = []
    for name, parsed in parsed_candidates.items():
        for ref in manifest['references']:
            reference_matches.append({'candidate_id': name, 'reference': ref['name'],
                                      **compare(parsed, parse(ref['dsl']))})
    report = {'version': 1, 'ok': all(r['valid'] for r in results),
              'candidates': results, 'comparisons': comparisons,
              'reference_matches': reference_matches,
              'review_flags': [{'a': c['a'], 'b': c['b'],
                                'reason': 'Identical kick/snare score. Review whether these offer different rhythmic ideas.'}
                               for c in comparisons if c['same_backbone']],
              'judge_preferences': manifest['preferences'],
              'audition_candidates': [r['candidate_id'] for r in results if r['valid']],
              'policy': 'Keep every valid candidate, including the wildcard. Similarity is advisory; no automatic winner.',
              'quality': 'unassessed; requires listening through the destination kit',
              'report': str(run / 'report.json')}
    write_json(run / 'report.json', report)
    return report


def feedback(path, record):
    workspace, manifest = load_workspace(path)
    if not isinstance(record, dict):
        raise ValueError('feedback must be an object')
    name = record.get('candidate_id')
    if name not in manifest['candidates']:
        raise ValueError('Unknown candidate_id')
    if record.get('usefulness') not in {'use', 'revise', 'reject'}:
        raise ValueError('usefulness must be use, revise or reject')
    if record.get('novelty') not in {'familiar', 'new', 'unsure'}:
        raise ValueError('novelty must be familiar, new or unsure')
    text_value(record.get('reason'), 'reason')
    # Bind feedback to the exact auditioned evaluation, never a subsequently edited DSL.
    report_path = local_file(workspace, text_value(record.get('report'), 'report'))
    report = read_json(report_path)
    if not isinstance(report, dict) or not isinstance(report.get('candidates'), list):
        raise ValueError('Invalid evaluation report')
    candidate = next((c for c in report['candidates'] if c['candidate_id'] == name and c['valid']), None)
    if not candidate:
        raise ValueError('Feedback requires a valid evaluated candidate')
    scope = record.get('scope', 'request')
    if scope not in {'request', 'project', 'global'}:
        raise ValueError('Feedback scope must be request, project or global')
    if scope == 'global' and record.get('confirmed') is not True:
        raise ValueError('Global feedback requires explicit confirmed: true')
    saved = {k: record[k] for k in ('candidate_id', 'usefulness', 'novelty', 'reason')}
    saved.update(scope=scope, context=manifest['context'], dsl_sha256=candidate['dsl_sha256'],
                 report=str(report_path), source='user_feedback',
                 preference_promotion='none; observations do not automatically become rules')
    if scope == 'global':
        saved['confirmed'] = True
    output = workspace / ('feedback-' + uuid.uuid4().hex + '.json')
    write_json(output, saved)
    return {'ok': True, 'path': str(output), 'feedback': saved}


def run(action, path, output=None, record=None):
    if action == 'prepare':
        if not output:
            raise ValueError('prepare requires output')
        return prepare(read_json(path), output, Path(path).resolve().parent)
    if action == 'evaluate':
        return evaluate(path)
    if action == 'feedback':
        return feedback(path, record)
    raise ValueError('Unknown workshop action')


def cli(args):
    try:
        result = run(args.action, args.path, args.output,
                     read_json(args.feedback) if args.feedback else None)
        print(json.dumps(result, indent=2))
        return 0 if result['ok'] else 1
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        return 1


def add_parser(sub):
    parser = sub.add_parser('drum-workshop', help='Prepare and compare drum ideas without changing REAPER')
    parser.add_argument('action', choices=['prepare', 'evaluate', 'feedback'])
    parser.add_argument('path', help='Brief JSON for prepare; workshop folder otherwise')
    parser.add_argument('--output', help='New workshop folder for prepare')
    parser.add_argument('--feedback', help='User feedback JSON for feedback')
    parser.set_defaults(func=cli)
