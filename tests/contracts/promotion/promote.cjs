const assert = require('node:assert/strict');
const test = require('node:test');
const promote = require('../../../.github/scripts/promote.cjs');

function fixture(options = {}) {
  const writes = [];
  const refs = [];
  const outputs = {};
  const context = {
    repo: { owner: 'owner', repo: 'repo' }, eventName: 'push', ref: 'refs/heads/dev',
    sha: 'graded', payload: { repository: { full_name: 'owner/repo' } },
    serverUrl: 'https://github.com', runId: 42,
    ...options.context,
  };
  const commit = (sha, tree) => ({ sha, commit: { tree: { sha: tree } } });
  const pull = { number: 7, head: { sha: options.pullHead || 'graded', repo: { full_name: 'owner/repo' } },
    base: { ref: 'main' }, html_url: 'https://github.com/owner/repo/pull/7' };
  let reads = 0;
  let readiness = 0;
  const github = { rest: {
    git: {
      getRef: async ({ ref }) => {
        if (ref === 'heads/dev') return { data: { object: { sha: ++reads === 1 ? options.dev || 'graded' : options.moved || 'graded' } } };
        if (options.snapshot) return { data: { object: { sha: options.snapshot } } };
        throw Object.assign(new Error('Not Found'), { status: 404 });
      },
      createRef: async args => { refs.push(['create', args]); },
      deleteRef: async args => {
        refs.push(['delete', args]);
        if (options.deleted) throw Object.assign(new Error(options.deleted === 422 ? 'Reference does not exist' : 'Not Found'), { status: options.deleted });
        if (options.deleteFailed) throw Object.assign(new Error('Server error'), { status: 500 });
      },
    },
    repos: { getCommit: async ({ ref }) => ({ data: ref === 'graded' ? commit('graded', 'tree') :
      ref === 'main' ? commit('main', options.already ? 'tree' : 'old') : commit('rebased', options.wrongTree ? 'wrong' : 'tree') }) },
    pulls: {
      get: async () => ({ data: { ...pull, mergeable: options.conflict ? false : true,
        mergeable_state: options.pending || options.waitOnce && readiness++ === 0 ? 'blocked' : 'clean' } }),
      list: async () => ({ data: options.existing ? [pull] : [] }),
      create: async args => { writes.push(['create', args]); return { data: pull }; },
      merge: async args => {
        writes.push(['merge', args]);
        if (options.refused) throw new Error('Required checks are pending');
        return { data: { merged: !options.unmerged, sha: 'rebased' } };
      },
    },
  } };
  const summary = { addHeading() { return this; }, addTable() { return this; }, async write() {} };
  const core = { setOutput: (key, value) => { outputs[key] = value; }, summary };
  return { github, context, core, writes, refs, outputs, sleep: async () => {} };
}

test('promotes exactly the graded head with rebase and records both revisions', async () => {
  const f = fixture();
  await promote(f);
  assert.equal(f.writes[0][1].base, 'main');
  assert.equal(f.writes[0][1].head, 'promotion/graded');
  assert.deepEqual(f.refs, [
    ['create', { owner: 'owner', repo: 'repo', ref: 'refs/heads/promotion/graded', sha: 'graded' }],
    ['delete', { owner: 'owner', repo: 'repo', ref: 'heads/promotion/graded' }],
  ]);
  assert.match(f.writes[0][1].body, /graded/);
  assert.deepEqual(f.writes[1], ['merge', { owner: 'owner', repo: 'repo', pull_number: 7, sha: 'graded', merge_method: 'rebase' }]);
  assert.equal(f.outputs.main, 'rebased');
  assert.equal(f.outputs.graded, 'graded');
});

for (const context of [
  { eventName: 'workflow_dispatch' }, { eventName: 'pull_request' },
  { ref: 'refs/heads/diffcheck/plant' }, { payload: { repository: { full_name: 'foreign/repo' } } },
]) {
  test(`ignores an untrusted trigger ${JSON.stringify(context)}`, async () => {
    const f = fixture({ context });
    await promote(f);
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.outputs, {});
  });
}

test('promotes the graded revision after dev has advanced', async () => {
  const f = fixture({ dev: 'newer' });
  await promote(f);
  assert.equal(f.writes[0][1].head, 'promotion/graded');
  assert.equal(f.writes[1][1].sha, 'graded');
  assert.equal(f.outputs.graded, 'graded');
  assert.equal(f.outputs.main, 'rebased');
});

test('reuses an existing promotion pull request', async () => {
  const f = fixture({ existing: true });
  await promote(f);
  assert.deepEqual(f.writes.map(([kind]) => kind), ['merge']);
});

for (const status of [404, 422]) {
  test(`automatic branch deletion does not prevent release outputs (${status})`, async () => {
    const f = fixture({ deleted: status });
    await promote(f);
    assert.equal(f.outputs.main, 'rebased');
    assert.equal(f.outputs.graded, 'graded');
  });
}

test('snapshot deletion failures are reported', async () => {
  const f = fixture({ deleteFailed: true });
  await assert.rejects(promote(f), /Server error/);
  assert.equal(f.outputs.main, undefined);
});

test('reuses a matching snapshot left before pull request creation', async () => {
  const f = fixture({ snapshot: 'graded' });
  await promote(f);
  assert.deepEqual(f.refs.map(([kind]) => kind), ['delete']);
  assert.equal(f.outputs.main, 'rebased');
});

test('rejects a snapshot that does not identify the graded revision', async () => {
  const f = fixture({ snapshot: 'ungraded' });
  await assert.rejects(promote(f), /snapshot/);
  assert.deepEqual(f.writes, []);
  assert.deepEqual(f.refs, []);
});

test('waits for GitHub to compute a clean merge', async () => {
  const f = fixture({ waitOnce: true });
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
});

for (const options of [{ conflict: true }, { pending: true }]) {
  test(`never merges a blocked pull request ${JSON.stringify(options)}`, async () => {
    const f = fixture({ existing: true, ...options });
    await assert.rejects(promote(f));
    assert.deepEqual(f.writes, []);
    assert.equal(f.outputs.main, undefined);
  });
}

test('promotes the graded revision while dev keeps moving', async () => {
  const f = fixture({ existing: true, moved: 'newer' });
  await promote(f);
  assert.deepEqual(f.writes.map(([kind]) => kind), ['merge']);
  assert.equal(f.writes[0][1].sha, 'graded');
  assert.equal(f.outputs.main, 'rebased');
});

test('a moved pull request cannot merge', async () => {
  const f = fixture({ existing: true, pullHead: 'ungraded' });
  await assert.rejects(promote(f), /graded dev head/);
  assert.deepEqual(f.writes, []);
});

for (const options of [{ refused: true }, { wrongTree: true }, { unmerged: true }]) {
  test(`a rejected merge or changed tree cannot release ${JSON.stringify(options)}`, async () => {
    const f = fixture(options);
    await assert.rejects(promote(f));
    assert.equal(f.outputs.main, undefined);
  });
}

test('a repeated promotion does not open or merge another pull request', async () => {
  const f = fixture({ already: true });
  await promote(f);
  assert.deepEqual(f.writes, []);
  assert.equal(f.outputs.main, 'main');
});
