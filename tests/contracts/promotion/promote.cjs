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
  const runUrl = 'https://github.com/owner/repo/actions/runs/42';
  const jobs = [{ id: 71, run_id: 42, name: 'promote / promote', status: 'in_progress',
    check_run_url: 'https://api.github.com/repos/owner/repo/check-runs/900' }];
  const checks = [
    { id: 901, name: 'Gate — Required', status: 'completed', conclusion: 'success', details_url: `${runUrl}/job/72` },
    { id: 900, name: 'promote / promote', status: 'in_progress', conclusion: null,
      url: jobs[0].check_run_url, details_url: `${runUrl}/job/71` },
  ];
  const statuses = [];
  const pages = [];
  let reads = 0;
  let readiness = 0;
  const github = { rest: {
    actions: { listJobsForWorkflowRun: async () => ({ data: { jobs: jobs.slice(0, 100) } }) },
    checks: { listForRef: async () => ({ data: { check_runs: checks.slice(0, 100) } }) },
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
    repos: {
      getCommit: async ({ ref }) => ({ data: ref === 'graded' ? commit('graded', 'tree') :
        ref === 'main' ? commit('main', options.already ? 'tree' : 'old') : commit('rebased', options.wrongTree ? 'wrong' : 'tree') }),
      listCommitStatusesForRef: async () => ({ data: statuses.slice(0, 100) }),
    },
    pulls: {
      get: async () => ({ data: { ...pull, mergeable: options.conflict ? false : true, rebaseable: !options.nonrebaseable,
        mergeable_state: options.mergeState || (options.pending || options.waitOnce && readiness++ === 0 ? 'blocked' : 'clean') } }),
      list: async () => ({ data: options.existing ? [pull] : [] }),
      create: async args => { writes.push(['create', args]); return { data: pull }; },
      merge: async args => {
        writes.push(['merge', args]);
        if (options.refused) throw new Error('Required checks are pending');
        return { data: { merged: !options.unmerged, sha: 'rebased' } };
      },
    },
  } };
  github.paginate = async (method, args) => {
    pages.push(args);
    if (method === github.rest.actions.listJobsForWorkflowRun) return jobs;
    if (method === github.rest.checks.listForRef) return checks;
    if (method === github.rest.repos.listCommitStatusesForRef) return statuses;
    throw new Error('Unexpected paginated endpoint');
  };
  const summary = { addHeading() { return this; }, addTable() { return this; }, async write() {} };
  const core = { setOutput: (key, value) => { outputs[key] = value; }, summary };
  return { github, context, core, writes, refs, outputs, jobs, checks, statuses, pages, sleep: async () => {} };
}

test('promotes exactly the graded head with linear history and records both revisions', async () => {
  const f = fixture();
  await promote(f);
  assert.equal(f.writes[0][1].base, 'main');
  assert.equal(f.writes[0][1].head, 'promotion/graded');
  assert.deepEqual(f.refs, [
    ['create', { owner: 'owner', repo: 'repo', ref: 'refs/heads/promotion/graded', sha: 'graded' }],
    ['delete', { owner: 'owner', repo: 'repo', ref: 'heads/promotion/graded' }],
  ]);
  assert.match(f.writes[0][1].body, /graded/);
  assert.equal(f.writes[1][0], 'merge');
  const { merge_method, ...request } = f.writes[1][1];
  assert.ok(['rebase', 'squash'].includes(merge_method));
  assert.deepEqual(request, { owner: 'owner', repo: 'repo', pull_number: 7, sha: 'graded' });
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

test('squashes the graded integration history when GitHub cannot rebase it', async () => {
  const f = fixture({ nonrebaseable: true });
  await promote(f);
  assert.equal(f.writes[1][1].merge_method, 'squash');
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

const selfWaitFixture = () => fixture({ existing: true, mergeState: 'unstable' });

test('admits only its running check after the current run grade passed', async () => {
  const f = selfWaitFixture();
  f.checks.push({ name: 'unit-selected', status: 'completed', conclusion: 'skipped' });
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
  assert.equal(f.writes[0][1].sha, 'graded');
  assert.ok(f.pages.every(args => args.per_page === 100));
  assert.equal(f.pages.filter(args => args.ref === 'graded').length, 2);
  assert.equal(f.pages.find(args => args.run_id === 42).filter, 'latest');
});

test('uses the latest successful status for each context', async () => {
  const f = selfWaitFixture();
  f.statuses.push({ context: 'policy', state: 'success' }, { context: 'policy', state: 'failure' });
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
});

for (const outcome of ['queued', 'in_progress', 'failure', 'cancelled', 'timed_out', 'neutral', 'action_required', 'stale']) {
  test(`another nonpassing check blocks promotion: ${outcome}`, async () => {
    const f = selfWaitFixture();
    const pending = outcome === 'queued' || outcome === 'in_progress';
    f.checks.push({ name: 'another check', status: pending ? outcome : 'completed', conclusion: pending ? null : outcome });
    await assert.rejects(promote(f));
    assert.deepEqual(f.writes, []);
  });
}

for (const state of ['pending', 'failure', 'error']) {
  test(`a nonpassing effective commit status blocks promotion: ${state}`, async () => {
    const f = selfWaitFixture();
    f.statuses.push({ context: 'policy', state }, { context: 'policy', state: 'success' });
    await assert.rejects(promote(f));
    assert.deepEqual(f.writes, []);
  });
}

for (const [name, alter] of [
  ['missing grade', f => f.checks.shift()],
  ['skipped grade', f => { f.checks[0].conclusion = 'skipped'; }],
  ['failed grade', f => { f.checks[0].conclusion = 'failure'; }],
  ['pending grade', f => { f.checks[0].status = 'queued'; }],
  ['another run grade', f => { f.checks[0].details_url = 'https://github.com/owner/repo/actions/runs/41/job/72'; }],
  ['missing current job', f => { f.jobs.length = 0; }],
  ['duplicate current jobs', f => { f.jobs.push({ ...f.jobs[0], id: 73 }); }],
  ['another run job', f => { f.jobs[0].run_id = 41; }],
  ['another job id', f => { f.jobs[0].id = 73; }],
  ['another check url', f => { f.jobs[0].check_run_url += '/foreign'; }],
  ['another check name', f => { f.checks[1].name = 'another job'; }],
  ['another run details', f => { f.checks[1].details_url = 'https://github.com/owner/repo/actions/runs/41/job/71'; }],
  ['completed current job', f => { f.jobs[0].status = 'completed'; }],
  ['failed current check', f => { f.checks[1].status = 'completed'; f.checks[1].conclusion = 'failure'; }],
]) {
  test(`an unverified current grade or job blocks promotion: ${name}`, async () => {
    const f = selfWaitFixture();
    alter(f);
    await assert.rejects(promote(f));
    assert.deepEqual(f.writes, []);
  });
}

test('reads a failing check beyond the first hundred results', async () => {
  const f = selfWaitFixture();
  f.checks.push(...Array.from({ length: 110 }, (_, id) => ({ id, name: `extra-${id}`, status: 'completed', conclusion: 'success' })),
    { name: 'late failure', status: 'completed', conclusion: 'failure' });
  await assert.rejects(promote(f));
  assert.deepEqual(f.writes, []);
});

test('reads a failing status beyond the first hundred contexts', async () => {
  const f = selfWaitFixture();
  f.statuses.push(...Array.from({ length: 110 }, (_, id) => ({ context: `extra-${id}`, state: 'success' })),
    { context: 'late failure', state: 'failure' });
  await assert.rejects(promote(f));
  assert.deepEqual(f.writes, []);
});

test('finds the exact current job beyond the first hundred jobs', async () => {
  const f = selfWaitFixture();
  f.jobs.unshift(...Array.from({ length: 110 }, (_, id) => ({ id, run_id: 42, name: `extra-${id}`, status: 'completed' })));
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
});
