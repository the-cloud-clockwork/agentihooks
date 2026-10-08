import json
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_artifacts, ledger_core as core, ledger_publish, ledger_tasks
from tests.swarm_ledger.test_plan_kind import add, plan_ledger, update
from tests.swarm_ledger.test_plan_publish import cli, task

PLAN = '# Plan\n\n## Build\n<!-- slice: first -->\n### First\nOne\n<!-- slice: second -->\n### Second\nTwo\n<!-- slice: third -->\n### Third\nThree\n## Ship\nOther\n'


@pytest.fixture
def published(plan_ledger, tmp_path, monkeypatch, capsys):
    core.sync(plan_ledger, ops=[{'op': 'join', 'id': 'join', 'by': 'planner', 'role': 'member'}])
    path = tmp_path / 'plan.md'
    path.write_text(PLAN)
    monkeypatch.delenv('AGENTIHOOKS_SWARM_TASK', raising=False)
    monkeypatch.setattr(ledger.ledger_publish, 'has_issues', lambda repo, run=None: False)
    monkeypatch.setattr(ledger, 'upload_artifact', lambda slug, name, path, request: ledger_artifacts.store(slug, 'plan.md', PLAN.encode()))
    cli(monkeypatch, plan_ledger, 'publish-plan', str(path), '--phase', 'p1')
    capsys.readouterr()
    return plan_ledger


def test_issue_publication_always_stores_artifact():
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({'hasIssuesEnabled': True}) if argv[1] == 'repo' else 'https://github.com/acme/app/issues/1\n')
    saved = []
    ledger_publish.publish('plan.md', 'Build', 'acme/app', lambda *args: saved.append(args) or 'http://localhost/artifacts/demo/file.md', run)
    assert saved == [('plan.md', 'Build')]
    assert calls[1] == ['gh', 'issue', 'create', '--title', 'Build', '--body', 'Build\n\nhttp://localhost/artifacts/demo/file.md', '--repo', 'acme/app']


def test_publication_and_three_computed_ranges(published, monkeypatch):
    state = core.sync(published)[0]
    [artifact] = state['artifacts']
    assert artifact['plan'] is True
    ref = state['phases'][0]['plan_ref']
    assert ref == {'artifact': f'{ledger.BASE}/artifacts/{published}/{artifact["file"]["id"]}', 'lines': '3-12'}
    for name, expected in [('first', '4-6'), ('second', '7-9'), ('third', '10-12')]:
        cli(monkeypatch, published, 'task', 'add', name, name.title(), '--phase', 'p1', '--plan-slice', name)
        state = core.sync(published)[0]
        assert task(state, name)['plan_lines'] == expected
        assert task(state, name)['plan_slice'] == name
    add(published, 'plan', lane='plan', kind='plan')
    state, rejected = update(published, state='done', proof={'slice': 'first,second,third'})
    assert rejected == []
    assert task(state, 'plan')['state'] == 'done'


def test_missing_anchor_refused(published):
    state, rejected = add(published, 'missing', plan_slice='missing')
    assert rejected == ['add-missing']
    assert 'slice anchor missing is missing' in state['_meta']['warnings'][0]
    assert not any(t['id'] == 'missing' for t in state['tasks'])


def test_slice_without_range_refused(plan_ledger):
    add(plan_ledger, 'plan', lane='plan', kind='plan')
    add(plan_ledger, 'first', plan_url='https://github.com/acme/app/issues/1')
    state, rejected = update(plan_ledger, state='done', proof={'slice': 'first'})
    assert rejected == ['finish-plan']
    assert task(state, 'plan')['state'] == 'open'
