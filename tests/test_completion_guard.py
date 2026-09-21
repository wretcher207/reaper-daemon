import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('completion_guard', Path(__file__).parents[1]/'.codex/completion_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def event(root, name, **values):
    return dict(session_id='test-session', cwd=str(root), hook_event_name=name, turn_id='test-turn', **values)


@pytest.mark.parametrize('reply', [
    "You're right. I'll fix it now.",
    "No. I described the guard without building it. I'll build and test it now.",
    "I should establish a convincing groove before extending it.",
])
def test_promise_only_finish_is_blocked(tmp_path, reply):
    storage = tmp_path/'state'
    guard.handle(event(tmp_path, 'UserPromptSubmit', prompt='Please fix this'), storage, tmp_path)
    result = guard.handle(event(tmp_path, 'Stop', last_assistant_message=reply), storage, tmp_path)
    assert result['decision'] == 'block'


@pytest.mark.parametrize('prompt', [
    'WHAT DO WE NEED TO DO!??! HOW DO WE MAKE YOU EFFECTIVE?',
    'Why did you stop?', 'Stop changing the drums.', 'Pause.',
])
def test_discussion_and_stop_do_not_resume_project(tmp_path, prompt):
    storage = tmp_path/'state'
    guard.handle(event(tmp_path, 'UserPromptSubmit', prompt='Fix the drums'), storage, tmp_path)
    guard.handle(event(tmp_path, 'UserPromptSubmit', prompt=prompt), storage, tmp_path)
    assert guard.handle(event(tmp_path, 'Stop', last_assistant_message="I'll fix the drums."), storage, tmp_path) == {}


def test_claiming_done_does_not_satisfy_registered_check(tmp_path):
    storage = tmp_path/'state'
    guard.handle(event(tmp_path, 'UserPromptSubmit', prompt='Build the guard'), storage, tmp_path)
    key = hashlib.sha256(b'test-session').hexdigest()[:24]
    guard.save(storage/(key+'.contract.json'), {'version':1, 'checks':[
        {'kind':'json_equals','path':'verified.json','keys':['passed'],'expected':True}]})
    attempt = event(tmp_path, 'Stop', last_assistant_message='Done. Everything passed.')
    assert guard.handle(attempt, storage, tmp_path)['decision'] == 'block'
    (tmp_path/'verified.json').write_text('{"passed":true}')
    assert guard.handle(attempt, storage, tmp_path) == {}


def test_bounded_continuation_and_interrupt(tmp_path):
    storage = tmp_path/'state'
    guard.handle(event(tmp_path, 'UserPromptSubmit', prompt='Fix it'), storage, tmp_path)
    stop = event(tmp_path, 'Stop', last_assistant_message="I'll fix it.")
    for _ in range(2):
        result = guard.handle(stop, storage, tmp_path)
        assert result['decision']=='block'
        guard.handle(event(tmp_path,'UserPromptSubmit',prompt=result['reason']),storage,tmp_path)
    assert 'decision' not in guard.handle(stop,storage,tmp_path)
    guard.handle(event(tmp_path,'Interrupt'),storage,tmp_path)
    assert guard.handle(stop,storage,tmp_path)=={}


def test_other_workspaces_and_sessions_are_unaffected(tmp_path):
    storage=tmp_path/'state'
    root=tmp_path/'repo'; root.mkdir()
    assert guard.handle(event(tmp_path,'UserPromptSubmit',prompt='Fix it'),storage,root)=={}
    guard.handle(event(root,'UserPromptSubmit',prompt='Fix it'),storage,root)
    stop=event(root,'Stop',last_assistant_message="I'll fix it.")
    stop['session_id']='different'
    assert guard.handle(stop,storage,root)=={}


def test_evidence_paths_cannot_escape_workspace(tmp_path):
    missing=guard.evidence({'version':1,'checks':[{'kind':'sha256','path':'../secret','expected':'anything'}]},tmp_path)
    assert missing


def test_normal_completed_action_and_educational_answer_pass(tmp_path):
    storage=tmp_path/'state'
    guard.handle(event(tmp_path,'UserPromptSubmit',prompt='Fix it'),storage,tmp_path)
    assert guard.handle(event(tmp_path,'Stop',last_assistant_message='Applied and verified all notes.'),storage,tmp_path)=={}
    guard.handle(event(tmp_path,'UserPromptSubmit',prompt='Explain how this works'),storage,tmp_path)
    assert guard.handle(event(tmp_path,'Stop',last_assistant_message='I need to check this example to explain it.'),storage,tmp_path)=={}
