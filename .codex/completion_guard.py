"""Project-scoped Stop hook. Checks promises and explicit completion evidence.

No model calls, commands from a transcript, project mutations or network access.
The hook may request at most two continuations per real user turn. Discussion,
user stops and interrupts suspend the action gate. This is not a semantic judge.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'state' / 'completion-guard'
PROMISE = re.compile(r"\b(?:I(?:'|’)ll|I will|I(?:'|’)m going to|I should|I need to|let me)\s+(?:actually\s+|now\s+)?(?:fix|implement|build|write|edit|replace|change|check|verify|test|do|start|make|apply|finish|inspect|run|create|continue|correct|establish|draft|read)\b", re.I)
ACKNOWLEDGEMENT = re.compile(r"\b(?:you(?:'|’)re right|you are right|you shouldn(?:'|’)t have|you(?:'|’)ve consistently wanted|I stopped at|I treated .* as a completed response)\b", re.I)
ACTION = re.compile(r'\b(fix|implement|build|write|edit|replace|change|verify|test|apply|finish|create|continue|redo|re-do|install|enable)\b', re.I)
CORRECTION = re.compile(r"\b(?:still (?:broken|wrong)|pretty much the same|almost no different|too much|not what I|doesn['’]?t work|didn['’]?t (?:fix|change)|zero accent)\b", re.I)
DISCUSSION = re.compile(r'\b(?:why did you|why would I|how (?:do|can) we (?:make|stop|prevent)|how (?:do|can) I make you|what do we (?:need to|gotta|have to) do|explain|recommend|best way)\b', re.I)
STOP = re.compile(r"^\s*(?:(?:please|no)[, .!]*\s*)?(?:stop|pause|cancel|don['’]?t (?:edit|change|continue)|do not (?:edit|change|continue))\b", re.I)


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     delete=False, suffix='.tmp') as stream:
        json.dump(value, stream, indent=2)
        temporary = Path(stream.name)
    temporary.replace(path)


def mode(prompt, previous='discussion'):
    if STOP.search(prompt):
        return 'stopped'
    if DISCUSSION.search(prompt):
        return 'discussion'
    if ACTION.search(prompt) or CORRECTION.search(prompt):
        return 'action'
    if prompt.strip().lower() in {'ok', 'okay', 'yes', 'go ahead', 'do it'}:
        return previous
    # A status question inherits a pending action but ordinary conversation does not.
    if re.search(r'\b(?:done|finished|completed|stopped again)\b', prompt, re.I):
        return previous
    return 'discussion'


def evidence(contract, root):
    """Read-only allowlisted checks. The contract cannot execute arbitrary commands."""
    if not isinstance(contract, dict) or contract.get('version') != 1:
        return ['Invalid completion contract']
    checks = contract.get('checks', [])
    if not isinstance(checks, list) or not 1 <= len(checks) <= 32:
        return ['Contract requires 1 to 32 checks']
    missing = []
    for check in checks:
        if not isinstance(check, dict):
            missing.append('Invalid evidence check')
            continue
        path = (root / check.get('path', '')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            missing.append(f"Missing workspace evidence: {check.get('path')}")
            continue
        if path.stat().st_size > 8 * 1024 * 1024:
            missing.append(f'Evidence too large: {path.name}')
            continue
        data = path.read_bytes()
        kind = check.get('kind')
        if kind == 'sha256':
            good = hashlib.sha256(data).hexdigest() == check.get('expected')
        elif kind == 'changed':
            baseline = check.get('baseline_sha256')
            good = isinstance(baseline, str) and len(baseline) == 64 and hashlib.sha256(data).hexdigest() != baseline
        elif kind == 'json_equals':
            try:
                value = json.loads(data)
                for key in check['keys']:
                    value = value[key]
                good = value == check['expected']
            except (ValueError, KeyError, TypeError):
                good = False
        else:
            good = False
        if not good:
            missing.append(f'Evidence check failed: {check.get("path")} ({kind})')
    return missing


def handle(event, storage=STATE, root=ROOT):
    # A project-local hook must never apply to another workspace.
    cwd = Path(event.get('cwd') or root).resolve()
    if not cwd.is_relative_to(root):
        return {}
    session = event.get('session_id', '')
    if not session:
        return {}
    key = hashlib.sha256(session.encode()).hexdigest()[:24]
    state_path = storage / (key + '.json')
    state = read(state_path, {'mode': 'discussion', 'continuations': 0})
    kind = event.get('hook_event_name')
    if kind == 'UserPromptSubmit':
        prompt = event.get('prompt', '')
        # Continuations are new prompts. Don't reset the budget for our own prompt.
        if prompt.startswith('[completion-guard]'):
            return {}
        state.update(mode=mode(prompt, state['mode']), continuations=0, tool_calls=0,
                     turn_id=event.get('turn_id'), last_user_prompt=prompt[:4000])
        save(state_path, state)
        return {'hookSpecificOutput': {'hookEventName': kind, 'additionalContext':
                'Completion guard mode: ' + state['mode'] + '. '
                + ('Answer the current question. Do not resume project edits unless requested.'
                   if state['mode'] != 'action' else
                   'Carry the requested action through verification. Explanations and promises are not completion. '
                   'For objective checks, use a session-scoped completion contract; never fabricate evidence.')}}
    if kind == 'Interrupt':
        state['mode'] = 'stopped'
        save(state_path, state)
        return {}
    if kind == 'PostToolUse':
        state['tool_calls'] = state.get('tool_calls', 0) + 1
        save(state_path, state)
        return {}
    if kind != 'Stop' or state['mode'] != 'action':
        return {}
    message = event.get('last_assistant_message') or ''
    # Contract files are optional. Without one, only the narrow promise detector applies.
    contract = read(storage / (key + '.contract.json'))
    reasons = evidence(contract, root) if contract else []
    if PROMISE.search(message) or (ACKNOWLEDGEMENT.search(message) and not state.get('tool_calls')):
        reasons.append('The proposed final reply promises further task work instead of reporting its result.')
    if not reasons:
        return {}
    if state['continuations'] >= 2:
        return {'systemMessage': 'Completion guard retry limit reached. Pending checks: ' + '; '.join(reasons)}
    state['continuations'] += 1
    save(state_path, state)
    result = {'decision': 'block', 'reason':
              '[completion-guard] The authorized action is still pending: ' + '; '.join(reasons)
              + ' Perform the missing work and verify it. Do not send another promise or apology as the result. '
                'If blocked, report the concrete blocker. Respect any newer user stop or discussion request.'}
    save(storage / (key + '.last-block.json'), {'event': kind, 'turn_id': event.get('turn_id'), **result})
    return result


def handle_global(event, storage=None):
    """User-level installation: isolate each workspace and session centrally."""
    if not event.get('cwd'):
        return {}
    root = Path(event['cwd']).resolve()
    base = storage if storage is not None else Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'state' / 'completion-guard'
    workspace = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:24]
    return handle(event, base / workspace, root)


def main():
    try:
        event = json.load(sys.stdin)
        print(json.dumps(handle_global(event) if '--global' in sys.argv else handle(event)))
    except Exception as exc:
        # Never trap the user in a continuation loop because the guard itself broke.
        print(json.dumps({'systemMessage': 'Completion guard error: ' + str(exc)}))


if __name__ == '__main__':
    main()
