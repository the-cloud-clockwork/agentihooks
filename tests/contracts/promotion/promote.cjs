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
  const commit = (sha, tree, message = '') => ({ sha, commit: { tree: { sha: tree }, message } });
  const pull = { number: 7, head: { sha: options.pullHead || 'snapshot', repo: { full_name: 'owner/repo' } },
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
  let elapsed = 0;
  let reads = 0;
  let readiness = 0;
  let mainReads = 0;
  const github = { rest: {
    actions: { listJobsForWorkflowRun: async () => ({ data: { jobs: jobs.slice(0, 100) } }) },
    checks: { listForRef: async () => ({ data: { check_runs: checks.slice(0, 100) } }) },
    git: {
      getRef: async ({ ref }) => {
        if (ref === 'heads/dev') return { data: { object: { sha: ++reads === 1 ? options.dev || 'graded' : options.moved || 'graded' } } };
        if (options.snapshot || options.existing) return { data: { object: { sha: options.snapshot || 'snapshot' } } };
        throw Object.assign(new Error('Not Found'), { status: 404 });
      },
      createCommit: async args => { refs.push(['commit', args]); return { data: { sha: 'snapshot' } }; },
      getCommit: async ({ commit_sha }) => ({ data: { sha: commit_sha, tree: { sha: commit_sha === 'snapshot' ? 'tree' : 'other' },
        parents: options.snapshotParents || [{ sha: 'main' }] } }),
      createRef: async args => { refs.push(['create', args]); },
      deleteRef: async args => {
        refs.push(['delete', args]);
        if (options.deleted) throw Object.assign(new Error(options.deleted === 422 ? 'Reference does not exist' : 'Not Found'), { status: options.deleted });
        if (options.deleteFailed) throw Object.assign(new Error('Server error'), { status: 500 });
      },
    },
    repos: {
      getCommit: async ({ ref }) => ({ data: ref === 'graded' ? commit('graded', 'tree') :
        ref === 'main' && options.mainMoves && ++mainReads > 1 ? commit('moved', 'old', `Promote green dev to main (#8)\n\nGraded dev revision: ${options.mainMoves}\n`) :
        ref === 'main' ? commit('main', options.already ? 'tree' : 'old', options.promoted ? `Promote green dev to main (#6)\n\nGraded dev revision: ${options.promoted}\n` : '') : commit('rebased', options.wrongTree ? 'wrong' : 'tree') }),
      listCommitStatusesForRef: async () => ({ data: statuses.slice(0, 100) }),
      compareCommits: async args => { refs.push(['compare', args]); return { data: { status: options.orders?.[args.base] || options.order || 'ahead' } }; },
    },
    pulls: {
      get: async () => ({ data: { ...pull, mergeable: options.conflict ? false : true, rebaseable: !options.nonrebaseable,
        mergeable_state: options.mergeState || (options.pending || options.waitOnce && readiness++ === 0 ? 'blocked' : 'clean') } }),
      list: async () => ({ data: options.existing ? [pull] : [] }),
      create: async args => { writes.push(['create', args]); return { data: pull }; },
      update: async args => { writes.push(['update', args]); },
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
  github.graphql = async () => ({ repository: { object: { statusCheckRollup: { contexts: {
    nodes: [], pageInfo: { hasNextPage: false, endCursor: null },
  } } } } });
  return { github, context, core, writes, refs, outputs, jobs, checks, statuses, pages,
    now: () => elapsed, sleep: async ms => { elapsed += ms; } };
}

test('waits past four minutes for the snapshot required checks to turn green', async () => {
  const f = fixture({ pending: true });
  const get = f.github.rest.pulls.get;
  f.github.rest.pulls.get = async args => {
    const result = await get(args);
    if (f.now() >= 327000) result.data.mergeable_state = 'clean';
    return result;
  };
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
  assert.ok(f.now() >= 327000);
  assert.ok(f.now() < 1800000);
});

for (const node of [
  { __typename: 'CheckRun', name: 'measure', status: 'COMPLETED', conclusion: 'FAILURE' },
  { __typename: 'StatusContext', context: 'measure', state: 'ERROR' },
]) {
  test(`fails immediately on a required snapshot failure ${node.__typename}`, async () => {
    const f = fixture({ pending: true });
    f.github.graphql = async () => ({ repository: { object: { statusCheckRollup: { contexts: {
      nodes: [{ ...node, isRequired: true }], pageInfo: { hasNextPage: false, endCursor: null },
    } } } } });
    await assert.rejects(promote(f), /Required snapshot check failed: measure/);
    assert.equal(f.now(), 0);
    assert.equal(f.outputs.main, undefined);
    assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
  });
}

test('stops pending snapshot checks at the thirty minute deadline', async () => {
  const f = fixture({ pending: true });
  await assert.rejects(promote(f), /thirty minutes/);
  assert.equal(f.now(), 1800000);
  assert.equal(f.outputs.main, undefined);
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
});

for (const state of ['EXPECTED', 'PENDING']) {
test(`waits for every required snapshot context even when mergeability reads clean: ${state}`, async () => {
  const f = fixture();
  const queries = [];
  f.github.graphql = async (query, variables) => {
    queries.push(variables);
    assert.match(query, /isRequired\(pullRequestNumber: \$number\)/);
    return { repository: { object: { statusCheckRollup: { contexts: {
      nodes: variables.cursor ? [
        { context: 'required status', isRequired: true, state: f.now() < 330000 ? state : 'SUCCESS' },
        { name: 'optional check', isRequired: false, status: 'COMPLETED', conclusion: 'FAILURE' },
      ] : [{ name: 'required check', isRequired: true, status: 'COMPLETED', conclusion: 'SUCCESS' }],
      pageInfo: { hasNextPage: !variables.cursor, endCursor: variables.cursor ? null : 'next-page' },
    } } } } };
  };
  await promote(f);
  assert.equal(f.now(), 330000);
  assert.equal(f.outputs.main, 'rebased');
  assert.deepEqual(queries[0], { owner: 'owner', repo: 'repo', snapshot: 'snapshot', number: 7, cursor: null });
  assert.equal(queries[1].cursor, 'next-page');
});
}

test('reads a required failure on a later page even while another check is pending', async () => {
  const f = fixture({ pending: true });
  f.github.graphql = async (query, { cursor }) => ({ repository: { object: { statusCheckRollup: { contexts: {
    nodes: cursor ? [{ name: 'late failure', isRequired: true, status: 'COMPLETED', conclusion: 'CANCELLED' }] :
      [{ name: 'queued check', isRequired: true, status: 'QUEUED', conclusion: null }],
    pageInfo: { hasNextPage: !cursor, endCursor: cursor ? null : 'next-page' },
  } } } } });
  await assert.rejects(promote(f), /Required snapshot check failed: late failure/);
  assert.equal(f.now(), 0);
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
});

test('promotes the graded tree as one commit on main and records both revisions', async () => {
  const f = fixture();
  await promote(f);
  assert.equal(f.writes[0][1].base, 'main');
  assert.equal(f.writes[0][1].head, 'promotion/graded');
  assert.match(f.refs[0][1].message, /graded/);
  assert.deepEqual(f.refs, [
    ['commit', { owner: 'owner', repo: 'repo', message: f.refs[0][1].message, tree: 'tree', parents: ['main'] }],
    ['create', { owner: 'owner', repo: 'repo', ref: 'refs/heads/promotion/graded', sha: 'snapshot' }],
    ['delete', { owner: 'owner', repo: 'repo', ref: 'heads/promotion/graded' }],
  ]);
  assert.match(f.writes[0][1].body, /graded/);
  assert.equal(f.writes[1][0], 'merge');
  const { merge_method, ...request } = f.writes[1][1];
  assert.ok(['rebase', 'squash'].includes(merge_method));
  assert.deepEqual(request, { owner: 'owner', repo: 'repo', pull_number: 7, sha: 'snapshot', commit_message: f.writes[0][1].body });
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
  assert.equal(f.writes[1][1].sha, 'snapshot');
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
  const f = fixture({ snapshot: 'snapshot' });
  await promote(f);
  assert.deepEqual(f.refs.map(([kind]) => kind), ['delete']);
  assert.equal(f.outputs.main, 'rebased');
});

test('rejects a snapshot that does not hold the graded tree', async () => {
  const f = fixture({ snapshot: 'ungraded' });
  await assert.rejects(promote(f), /snapshot/);
  assert.deepEqual(f.writes, []);
  assert.deepEqual(f.refs, []);
});

for (const snapshotParents of [[{ sha: 'older' }], [{ sha: 'main' }, { sha: 'graded' }], []]) {
  test(`rejects a snapshot that is not one commit on main ${JSON.stringify(snapshotParents)}`, async () => {
    const f = fixture({ snapshot: 'snapshot', snapshotParents });
    await assert.rejects(promote(f), /snapshot/);
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.refs, []);
  });
}

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
  assert.equal(f.writes[0][1].sha, 'snapshot');
  assert.equal(f.outputs.main, 'rebased');
});

test('a moved pull request cannot merge', async () => {
  const f = fixture({ existing: true, pullHead: 'ungraded' });
  await assert.rejects(promote(f), /snapshot commit/);
  assert.deepEqual(f.writes, []);
});

test('an open pull request without its snapshot branch cannot merge', async () => {
  const f = fixture({ existing: true });
  f.github.rest.git.getRef = async () => { throw Object.assign(new Error('Not Found'), { status: 404 }); };
  await assert.rejects(promote(f), /Not Found/);
  assert.deepEqual(f.writes, []);
  assert.deepEqual(f.refs, []);
});

const last = 'a'.repeat(40);

const newer = 'b'.repeat(40);

test('promotes a revision that descends from the last promotion', async () => {
  const f = fixture({ promoted: last, order: 'ahead' });
  await promote(f);
  assert.deepEqual(f.refs[0], ['compare', { owner: 'owner', repo: 'repo', base: last, head: 'graded' }]);
  assert.equal(f.outputs.main, 'rebased');
  assert.equal(f.outputs.superseded, undefined);
});

for (const order of ['behind', 'identical', 'diverged']) {
  test(`ends superseded when the graded revision is ${order} the last promotion`, async () => {
    const f = fixture({ promoted: last, order });
    await promote(f);
    assert.deepEqual(f.writes, []);
    assert.deepEqual(f.refs.map(([kind]) => kind), ['compare']);
    assert.equal(f.outputs.superseded, last);
    assert.equal(f.outputs.main, undefined);
  });
}

test('ends superseded when main takes a newer promotion during the wait', async () => {
  const f = fixture({ promoted: last, mainMoves: newer, orders: { [last]: 'ahead', [newer]: 'behind' } });
  await promote(f);
  assert.deepEqual(f.writes, [['create', f.writes[0][1]], ['update', { owner: 'owner', repo: 'repo', pull_number: 7, state: 'closed' }]]);
  assert.deepEqual(f.refs.map(([kind, args]) => kind === 'compare' ? args.base : kind), [last, 'commit', 'create', newer, 'delete']);
  assert.equal(f.outputs.superseded, newer);
  assert.equal(f.outputs.main, undefined);
});

test('never merges a stale snapshot when main moved without a newer promotion', async () => {
  const f = fixture({ promoted: last, mainMoves: newer, order: 'ahead' });
  await assert.rejects(promote(f), /main moved/);
  assert.deepEqual(f.writes.map(([kind]) => kind), ['create']);
  assert.equal(f.outputs.main, undefined);
});

test('a created pull request on another head cannot merge', async () => {
  const f = fixture({ pullHead: 'ungraded' });
  await assert.rejects(promote(f), /snapshot commit/);
  assert.deepEqual(f.writes.map(([kind]) => kind), ['create']);
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
  assert.equal(f.writes[0][1].sha, 'snapshot');
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

const current = 'e'.repeat(40);
const appPull = (number, graded, { login = 'promotion-app[bot]', full_name = 'owner/repo', ref = `promotion/${graded}` } = {}) =>
  ({ number, user: { login }, head: { ref, sha: `snapshot-${number}`, repo: full_name && { full_name } }, base: { ref: 'main' } });

function cleanupFixture(open, options = {}) {
  const writes = [];
  const tables = [];
  const ancestry = { ['a'.repeat(40)]: 'ahead', ['b'.repeat(40)]: 'ahead', ['c'.repeat(40)]: 'behind', ['d'.repeat(40)]: 'diverged' };
  const pulls = open.map(pull => ({ ...pull, state: 'open' }));
  const github = { rest: {
    pulls: {
      list: () => {},
      update: async args => {
        if (options.closeFails === args.pull_number) throw Object.assign(new Error('Validation Failed'), { status: 422 });
        writes.push(['close', args]);
        pulls.find(pull => pull.number === args.pull_number).state = 'closed';
      },
    },
    repos: { compareCommits: async ({ base, head }) => {
      if (options.compareFails === base) throw Object.assign(new Error('No common ancestor'), { status: 404 });
      return { data: { status: head === current ? ancestry[base] : 'diverged' } };
    } },
    issues: { createComment: async args => { writes.push(['comment', args]); } },
    git: {
      getRef: async ({ ref }) => {
        if (options.gone) throw Object.assign(new Error('Not Found'), { status: 404 });
        const pull = pulls.find(candidate => `heads/${candidate.head.ref}` === ref);
        return { data: { object: { sha: options.moved === pull.number ? 'elsewhere' : pull.head.sha } } };
      },
      deleteRef: async args => {
        writes.push(['delete', args]);
        if (options.deleted) throw Object.assign(new Error(options.deleted === 422 ? 'Reference does not exist' : 'Not Found'), { status: options.deleted });
      },
    },
  } };
  github.paginate = async (method, args) => {
    assert.equal(method, github.rest.pulls.list);
    assert.deepEqual(args, { owner: 'owner', repo: 'repo', state: 'open', base: 'main', per_page: 100 });
    return pulls.filter(pull => pull.state === 'open');
  };
  const summary = { addHeading() { return this; }, addTable(rows) { tables.push(rows); return this; }, async write() {} };
  const context = {
    repo: { owner: 'owner', repo: 'repo' }, eventName: 'push', ref: 'refs/heads/dev', sha: current,
    payload: { repository: { full_name: 'owner/repo' } }, serverUrl: 'https://github.com', runId: 42, ...options.context,
  };
  return { github, context, core: { summary }, app: 'app' in options ? options.app : 'promotion-app', writes, tables, pulls };
}

const census = () => [
  appPull(3, 'a'.repeat(40)), appPull(4, 'b'.repeat(40)), appPull(5, current), appPull(6, 'c'.repeat(40)),
  appPull(7, 'd'.repeat(40)), appPull(8, 'a'.repeat(40), { login: 'someone' }), appPull(9, 'a'.repeat(40), { full_name: 'fork/repo' }),
  appPull(10, 'a'.repeat(40), { full_name: null }), appPull(11, 'a'.repeat(40), { ref: 'feature' }),
  appPull(12, 'a'.repeat(39), { ref: `promotion/${'a'.repeat(39)}` }),
];
const kinds = f => f.writes.map(([kind, args]) => [kind, args.pull_number || args.issue_number || args.ref]);

test('closes only the abandoned attempts the promotion app opened and deletes their snapshots', async () => {
  const f = cleanupFixture(census());
  await promote.closeAbandoned(f);
  assert.deepEqual(kinds(f), [
    ['comment', 3], ['close', 3], ['delete', `heads/promotion/${'a'.repeat(40)}`],
    ['comment', 4], ['close', 4], ['delete', `heads/promotion/${'b'.repeat(40)}`],
  ]);
  assert.match(f.writes[0][1].body, new RegExp(`${current}.*${'a'.repeat(40)}`, 's'));
  assert.match(f.writes[0][1].body, /actions\/runs\/42/);
  assert.deepEqual(f.pulls.filter(pull => pull.state === 'open').map(pull => pull.number), [5, 6, 7, 8, 9, 10, 11, 12]);
  assert.deepEqual(f.tables[0].slice(1).map(([pull]) => pull), ['#3', '#4']);
});

test('repeating the cleanup changes nothing', async () => {
  const f = cleanupFixture(census());
  await promote.closeAbandoned(f);
  const first = f.writes.length;
  await promote.closeAbandoned(f);
  assert.equal(f.writes.length, first);
});

test('keeps a snapshot branch that moved away from its pull request head', async () => {
  const f = cleanupFixture([appPull(3, 'a'.repeat(40))], { moved: 3 });
  await promote.closeAbandoned(f);
  assert.deepEqual(kinds(f), [['comment', 3], ['close', 3]]);
});

for (const options of [{ gone: true }, { deleted: 404 }, { deleted: 422 }]) {
  test(`an already deleted abandoned snapshot is accepted ${JSON.stringify(options)}`, async () => {
    const f = cleanupFixture([appPull(3, 'a'.repeat(40))], options);
    await promote.closeAbandoned(f);
    assert.deepEqual(kinds(f).filter(([kind]) => kind === 'close'), [['close', 3]]);
  });
}

for (const options of [{ closeFails: 3 }, { compareFails: 'a'.repeat(40) }]) {
  test(`a cleanup failure is reported after the other attempts close ${JSON.stringify(options)}`, async () => {
    const f = cleanupFixture([appPull(3, 'a'.repeat(40)), appPull(4, 'b'.repeat(40))], options);
    await assert.rejects(promote.closeAbandoned(f), /#3/);
    assert.deepEqual(kinds(f).filter(([kind]) => kind !== 'comment'), [['close', 4], ['delete', `heads/promotion/${'b'.repeat(40)}`]]);
  });
}

test('stops at its time budget and leaves the rest for the next run without failing', async () => {
  const f = cleanupFixture(census());
  await promote.closeAbandoned({ ...f, deadline: Date.now() - 1 });
  assert.deepEqual(f.writes, []);
  assert.deepEqual(f.tables[0].slice(1), [['10 not examined', '', 'left for the next run, the cleanup time budget ran out']]);
});

test('refuses to choose pull requests without the promotion app identity', async () => {
  const f = cleanupFixture(census(), { app: '' });
  await assert.rejects(promote.closeAbandoned(f), /App/);
  assert.deepEqual(f.writes, []);
});

for (const context of [
  { eventName: 'workflow_dispatch' }, { eventName: 'pull_request' },
  { ref: 'refs/heads/diffcheck/plant' }, { payload: { repository: { full_name: 'foreign/repo' } } },
]) {
  test(`cleanup ignores an untrusted trigger ${JSON.stringify(context)}`, async () => {
    const f = cleanupFixture(census(), { context });
    await promote.closeAbandoned(f);
    assert.deepEqual(f.writes, []);
  });
}
