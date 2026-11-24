const assert = require('node:assert/strict');
const test = require('node:test');
const promote = require('../../../.github/scripts/promote.cjs');

const pageSize = per_page => Math.min(per_page ?? 30, 100);

const servedPage = (items, { page = 1, per_page }) => {
  const size = pageSize(per_page);
  return items.slice((page - 1) * size, page * size);
};

function fixture(options = {}) {
  const writes = [];
  const refs = [];
  const outputs = {};
  const infos = [];
  const context = {
    repo: { owner: 'owner', repo: 'repo' }, eventName: 'push', ref: 'refs/heads/dev',
    sha: 'graded', payload: { repository: { full_name: 'owner/repo' } },
    serverUrl: 'https://github.com', runId: 42,
    ...options.context,
  };
  const commit = (sha, tree, message = '') => ({ sha, commit: { tree: { sha: tree }, message } });
  const pull = { number: 7, head: { sha: options.pullHead || 'snapshot', repo: { full_name: 'owner/repo' } },
    base: { ref: 'main' }, html_url: 'https://github.com/owner/repo/pull/7' };
  const runUrl = `https://github.com/owner/repo/actions/runs/${context.runId}`;
  const jobs = [{ id: 71, run_id: context.runId, name: 'promote / promote', status: 'in_progress',
    check_run_url: 'https://api.github.com/repos/owner/repo/check-runs/900' },
    { id: 72, run_id: context.runId, name: 'Gate — Required', status: 'completed', conclusion: 'success' }];
  const checks = [
    { id: 901, name: 'Gate — Required', status: 'completed', conclusion: 'success', details_url: `${runUrl}/job/72` },
    { id: 900, name: 'promote / promote', status: 'in_progress', conclusion: null,
      url: jobs[0].check_run_url, details_url: `${runUrl}/job/71` },
  ];
  const statuses = [];
  const pages = [];
  const grades = [];
  let elapsed = 0;
  let reads = 0;
  let readiness = 0;
  let mainReads = 0;
  const github = { rest: {
    actions: {
      getWorkflowRun: async () => ({ data: { workflow_id: 10, run_number: context.runId, created_at: '2026-10-28T01:00:00Z' } }),
      listWorkflowRuns: async args => ({ data: { workflow_runs: servedPage([], args) } }),
      listJobsForWorkflowRun: async args => ({ data: { jobs: servedPage(jobs, args) } }),
    },
    checks: { listForRef: async args => ({ data: { check_runs: servedPage(checks, args) } }) },
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
      createCommitStatus: async args => { grades.push(args); },
      getCommit: async ({ ref }) => ({ data: ref === context.sha ? commit(context.sha, 'tree') :
        ref === 'main' && options.mainMoves && ++mainReads > 1 ? commit('moved', 'old', `Promote green dev to main (#8)\n\nGraded dev revision: ${options.mainMoves}\n`) :
        ref === 'main' ? commit('main', options.already ? 'tree' : 'old', options.promoted ? `Promote green dev to main (#6)\n\nGraded dev revision: ${options.promoted}\n` : '') : commit('rebased', options.wrongTree ? 'wrong' : 'tree') }),
      listCommitStatusesForRef: async args => ({ data: servedPage(args.ref === 'snapshot' ? [...grades].reverse() : statuses, args) }),
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
  github.paginate = async (method, args, map) => {
    pages.push(args);
    const lists = [github.rest.actions.listWorkflowRuns, github.rest.actions.listJobsForWorkflowRun, github.rest.checks.listForRef,
      github.rest.repos.listCommitStatusesForRef];
    if (!lists.includes(method)) throw new Error('Unexpected paginated endpoint');
    let items = [];
    let done = false;
    for (let page = 1; !done; page++) {
      const response = await method({ ...args, page });
      const data = Array.isArray(response.data) ? response.data : Object.values(response.data).find(Array.isArray);
      items = items.concat(map ? map({ ...response, data }, () => { done = true; }) : data);
      done ||= data.length < pageSize(args.per_page);
    }
    return items;
  };
  const summary = { addHeading() { return this; }, addTable() { return this; }, async write() {} };
  const core = { setOutput: (key, value) => { outputs[key] = value; }, summary, info: message => { infos.push(message); } };
  github.graphql = async () => ({ repository: { object: { statusCheckRollup: { contexts: {
    nodes: [], pageInfo: { hasNextPage: false, endCursor: null },
  } } } } });
  return { github, context, core, writes, refs, outputs, jobs, checks, statuses, pages, grades, infos,
    now: () => elapsed, sleep: async ms => { elapsed += ms; } };
}

test('reports the snapshot grade with the promotion app before merging', async () => {
  const f = fixture();
  f.github.rest.pulls.merge = async () => {
    assert.deepEqual(f.grades.map(grade => grade.state), ['pending', 'success']);
    return { data: { merged: true, sha: 'rebased' } };
  };
  await promote(f);
  assert.ok(f.grades.every(grade => grade.sha === 'snapshot' && grade.context === 'Gate — Required' &&
    grade.target_url === 'https://github.com/owner/repo/actions/runs/42'));
});

for (const status of ['queued', 'waiting', 'pending', 'in_progress']) {
  test(`a noncompleted triggering promote job merges on its first poll: ${status}`, async () => {
    const f = fixture();
    f.jobs[0].status = status;
    await promote(f);
    assert.equal(f.now(), 0);
    assert.equal(f.outputs.main, 'rebased');
    assert.equal(f.grades.at(-1).state, 'success');
    assert.equal(f.writes.find(([kind]) => kind === 'merge')[1].sha, 'snapshot');
  });
}

test('logs every changed grade poll once with all unsettled job statuses', async () => {
  const f = fixture();
  const current = f.jobs[0];
  const gate = f.jobs[1];
  gate.status = 'queued';
  const lint = { id: 75, run_id: 42, name: 'lint', status: 'queued', conclusion: null };
  f.jobs.push(lint);
  const sleep = f.sleep;
  f.sleep = async ms => {
    await sleep(ms);
    f.jobs.reverse();
    if (f.now() === 10000) {
      lint.status = 'in_progress';
      current.status = 'queued';
    }
    if (f.now() === 20000) {
      gate.status = lint.status = 'completed';
      lint.conclusion = 'success';
    }
  };
  await promote(f);
  assert.equal(f.now(), 20000);
  assert.deepEqual(f.infos.filter(message => message.startsWith('Promotion grade poll:')), [
    'Promotion grade poll: current promote jobs=1; unsettled jobs=Gate — Required queued, lint queued, promote / promote in_progress',
    'Promotion grade poll: current promote jobs=1; unsettled jobs=Gate — Required queued, lint in_progress, promote / promote queued',
    'Promotion grade poll: current promote jobs=1; unsettled jobs=promote / promote queued',
  ]);
});

for (const count of [0, 2]) {
  test(`logs a changing current promotion job count from ${count} to one`, async () => {
    const f = fixture();
    const current = f.jobs.shift();
    if (count === 2) f.jobs.push(current, { ...current, id: 73 });
    const sleep = f.sleep;
    f.sleep = async ms => {
      await sleep(ms);
      if (f.now() === 10000) {
        f.jobs.splice(0, f.jobs.length, f.jobs.find(job => job.name === 'Gate — Required'), { ...current, status: 'queued' });
      }
    };
    await promote(f);
    assert.equal(f.now(), 10000);
    assert.deepEqual(f.infos.filter(message => message.startsWith('Promotion grade poll:')), [
      `Promotion grade poll: current promote jobs=${count}; unsettled jobs=${count ? 'promote / promote in_progress, promote / promote in_progress' : 'none'}`,
      'Promotion grade poll: current promote jobs=1; unsettled jobs=promote / promote queued',
    ]);
  });
}

test('logs every unsettled job beyond one hundred without another jobs request', async () => {
  const f = fixture();
  f.jobs[1].status = 'queued';
  f.jobs.push(...Array.from({ length: 110 }, (_, id) => ({
    id: 100 + id, run_id: 42, name: `extra-${id}`, status: 'queued', conclusion: null,
  })));
  const sleep = f.sleep;
  f.sleep = async ms => {
    await sleep(ms);
    for (const job of f.jobs.filter(job => job.name !== 'promote / promote')) {
      job.status = 'completed';
      job.conclusion = 'success';
    }
  };
  await promote(f);
  const polls = f.infos.filter(message => message.startsWith('Promotion grade poll:'));
  assert.equal(polls.length, 2);
  assert.match(polls[0], /current promote jobs=1;/);
  assert.ok(polls[0].includes('Gate — Required queued'));
  assert.ok(polls[0].includes('promote / promote in_progress'));
  for (let id = 0; id < 110; id++) assert.ok(polls[0].includes(`extra-${id} queued`));
  assert.equal(polls[1], 'Promotion grade poll: current promote jobs=1; unsettled jobs=promote / promote in_progress');
  assert.equal(f.pages.filter(args => args.run_id === 42).length, 3);
});

test('keeps the snapshot grade pending until the original grade completes', async () => {
  const f = fixture();
  f.jobs[1].status = 'queued';
  const sleep = f.sleep;
  f.sleep = async ms => {
    assert.equal(f.grades.at(-1).state, 'pending');
    assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
    await sleep(ms);
    if (f.now() >= 15000) f.jobs[1].status = 'completed';
  };
  await promote(f);
  assert.equal(f.now(), 15000);
  assert.equal(f.grades.at(-1).state, 'success');
});

test('never reports a passing snapshot grade for a failed original grade', async () => {
  const f = fixture();
  f.jobs[1].conclusion = 'failure';
  await assert.rejects(promote(f));
  assert.ok(f.grades.length > 0);
  assert.ok(f.grades.every(grade => grade.state !== 'success'));
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
});

for (const conclusion of ['failure', 'cancelled', 'timed_out', 'neutral', 'action_required', 'stale']) {
  test(`fails at once when a job of its own graded run ended ${conclusion}`, async () => {
    const f = fixture();
    f.jobs.push({ id: 75, run_id: 42, name: 'lint', status: 'completed', conclusion });
    await assert.rejects(promote(f), new RegExp(`Graded run jobs did not pass: lint ${conclusion}$`));
    assert.equal(f.now(), 0);
    assert.equal(f.outputs.main, undefined);
    assert.ok(f.grades.every(grade => grade.state !== 'success'));
    assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
  });
}

test('fails at once on a red job of its graded run past the first page of jobs', async () => {
  const f = fixture();
  f.jobs.push(...Array.from({ length: 100 - f.jobs.length }, (_, id) => ({ id: 100 + id, run_id: 42, name: `green-${id}`,
    status: 'completed', conclusion: 'success' })), { id: 75, run_id: 42, name: 'lint', status: 'completed', conclusion: 'failure' });
  await assert.rejects(promote(f), /Graded run jobs did not pass: lint failure$/);
  assert.equal(f.now(), 0);
  assert.equal(f.outputs.main, undefined);
  assert.ok(f.grades.every(grade => grade.state !== 'success'));
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
});

test('waits for the reported snapshot grade to appear before merging', async () => {
  const f = fixture();
  const paginate = f.github.paginate;
  f.github.paginate = async (method, args) => args.ref === 'snapshot' && f.now() < 15000 ? [] : paginate(method, args);
  await promote(f);
  assert.equal(f.now(), 15000);
  assert.equal(f.grades.at(-1).state, 'success');
});

test('finds the reported snapshot grade behind a full first page of newer statuses', async () => {
  const f = fixture();
  const createCommitStatus = f.github.rest.repos.createCommitStatus;
  f.github.rest.repos.createCommitStatus = async args => {
    await createCommitStatus(args);
    if (args.state === 'success') f.grades.push(...Array.from({ length: 100 }, (_, id) => ({ ...args, context: `newer-${id}` })));
  };
  await promote(f);
  assert.equal(f.now(), 0);
  assert.equal(f.outputs.main, 'rebased');
});

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

function batchFixture({ ready = true, order = 'ahead', late = false, marker = {}, candidate = {}, unstable = false } = {}) {
  const f = fixture({ mergeState: unstable ? 'unstable' : undefined });
  const getCommit = f.github.rest.repos.getCommit;
  f.github.rest.repos.getCommit = async args => args.ref === newer ?
    { data: { sha: newer, commit: { tree: { sha: 'tree' }, message: '' } } } : getCommit(args);
  const paginate = f.github.paginate;
  let selections = 0;
  f.github.paginate = async (method, args) => {
    if (method === f.github.rest.actions.listWorkflowRuns) {
      selections++;
      assert.equal(args.workflow_id, 10);
      assert.equal(args.branch, 'dev');
      assert.equal(args.event, 'push');
      assert.equal(args.created, '>=2026-10-28T01:00:00Z');
      return [{ id: 43, run_number: 43, head_sha: newer, event: 'push', head_branch: 'dev', ...candidate }];
    }
    if (method === f.github.rest.actions.listJobsForWorkflowRun && args.run_id === 43) {
      return [{ name: 'Promotion ready', run_id: 43, status: 'completed',
        conclusion: ready && (!late || selections > 1) ? 'success' : 'failure', ...marker },
        { name: 'Gate — Required', run_id: 43, status: 'completed', conclusion: 'success' },
        { name: 'promote / promote', run_id: 43, status: 'completed', conclusion: 'cancelled' }];
    }
    return paginate(method, args);
  };
  f.github.rest.repos.compareCommits = async () => ({ data: { status: order } });
  return f;
}

test('skips the older grade and releases the newest verified green head', async () => {
  const f = batchFixture();
  await promote(f);
  assert.deepEqual(f.writes.map(([kind]) => kind), ['create', 'merge']);
  assert.equal(f.writes[0][1].head, `promotion/${newer}`);
  assert.match(f.writes[0][1].body, /actions\/runs\/43/);
  assert.equal(f.outputs.graded, newer);
  assert.equal(f.outputs.superseded, newer);
  assert.equal(f.outputs.main, 'rebased');
});

test('a late older green run replacing the newest pending run releases only the newest grade', async () => {
  const f = batchFixture();
  await promote(f);
  assert.equal(f.writes.filter(([kind]) => kind === 'merge').length, 1);
  assert.equal(f.writes[0][1].head, `promotion/${newer}`);
  assert.match(f.writes[0][1].body, new RegExp(`Graded dev revision: ${newer}`));
  assert.equal(f.outputs.graded, newer);
  assert.equal(f.outputs.superseded, newer);
  assert.equal(f.outputs.main, 'rebased');
});

test('never reads the jobs of a newer first attempt run still queued', async () => {
  const f = batchFixture({ candidate: { status: 'queued', run_attempt: 1 } });
  const paginate = f.github.paginate;
  f.github.paginate = async (method, args) => {
    assert.ok(!(method === f.github.rest.actions.listJobsForWorkflowRun && args.run_id === 43));
    return paginate(method, args);
  };
  await promote(f);
  assert.equal(f.outputs.graded, f.context.sha);
  assert.equal(f.outputs.superseded, undefined);
});

test('still reads a queued rerun that keeps its finished readiness job', async () => {
  const f = batchFixture({ candidate: { status: 'queued', run_attempt: 2 } });
  await promote(f);
  assert.equal(f.outputs.superseded, newer);
  assert.equal(f.outputs.graded, newer);
});

test('an unstable snapshot of a selected newer grade merges', async () => {
  const f = batchFixture({ unstable: true });
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
  assert.equal(f.outputs.graded, newer);
});

test('never releases a selected grade whose readiness is revoked before merge', async () => {
  const f = batchFixture();
  const paginate = f.github.paginate;
  let reads = 0;
  f.github.paginate = async (method, args) => {
    const result = await paginate(method, args);
    if (method === f.github.rest.actions.listJobsForWorkflowRun && args.run_id === 43 && ++reads > 1) {
      return result.map(job => ({ ...job, conclusion: 'failure' }));
    }
    return result;
  };
  await assert.rejects(promote(f), /no longer ready/);
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
  assert.equal(f.outputs.main, undefined);
});

test('fails at once naming a job already red in the picked newer grade', async () => {
  const f = batchFixture();
  const paginate = f.github.paginate;
  f.github.paginate = async (method, args) => {
    const result = await paginate(method, args);
    return method === f.github.rest.actions.listJobsForWorkflowRun && args.run_id === 43 ?
      [...result, { name: 'lint', run_id: 43, status: 'completed', conclusion: 'cancelled' }] : result;
  };
  await assert.rejects(promote(f), /Graded run jobs did not pass: lint cancelled$/);
  assert.equal(f.now(), 0);
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
  assert.equal(f.outputs.main, undefined);
});

test('releases the picked newer grade past an exempt side job it holds red', async () => {
  const f = batchFixture();
  const paginate = f.github.paginate;
  f.github.paginate = async (method, args) => {
    const result = await paginate(method, args);
    return method === f.github.rest.actions.listJobsForWorkflowRun && args.run_id === 43 ?
      [...result, { name: 'test-summary', run_id: 43, status: 'completed', conclusion: 'cancelled' }] : result;
  };
  await promote(f);
  assert.equal(f.now(), 0);
  assert.equal(f.outputs.graded, newer);
  assert.equal(f.outputs.main, 'rebased');
});

test('closes its snapshot when a newer grade becomes ready before merge', async () => {
  const f = batchFixture({ late: true });
  await promote(f);
  assert.deepEqual(f.writes.map(([kind]) => kind), ['create', 'update']);
  assert.equal(f.writes[1][1].state, 'closed');
  assert.equal(f.refs.at(-1)[0], 'delete');
  assert.equal(f.outputs.superseded, newer);
  assert.equal(f.outputs.main, undefined);
});

test('one release serves a batch and an older green grade completing afterwards skips', async () => {
  const latest = fixture({ context: { sha: newer, runId: 43 } });
  const older = batchFixture();
  let main = { sha: 'main', commit: { tree: { sha: 'old' }, message: '' } };
  for (const f of [latest, older]) {
    const get = f.github.rest.repos.getCommit;
    f.github.rest.repos.getCommit = async args => args.ref === 'main' ? { data: main } : get(args);
    f.github.rest.repos.compareCommits = async ({ base, head }) => {
      assert.equal(base, older.context.sha);
      assert.equal(head, latest.context.sha);
      return { data: { status: 'ahead' } };
    };
  }
  const merge = latest.github.rest.pulls.merge;
  latest.github.rest.pulls.merge = async args => {
    const result = await merge(args);
    main = { sha: result.data.sha, commit: { tree: { sha: 'tree' },
      message: `Graded dev revision: ${latest.context.sha}` } };
    return result;
  };
  await promote(latest);
  await promote(older);
  assert.notEqual(latest.context.sha, older.context.sha);
  assert.notEqual(latest.context.runId, older.context.runId);
  assert.equal(latest.outputs.graded, newer);
  assert.equal(older.outputs.graded, older.context.sha);
  assert.equal([...latest.writes, ...older.writes].filter(([kind]) => kind === 'merge').length, 1);
  assert.equal(latest.outputs.main, 'rebased');
  assert.equal(older.outputs.main, undefined);
  assert.equal(older.outputs.superseded, newer);
});

for (const options of [
  { ready: false }, { order: 'behind' }, { order: 'diverged' },
  { marker: { status: 'in_progress', conclusion: null } },
  { marker: { conclusion: 'cancelled' } }, { marker: { conclusion: 'skipped' } },
  { marker: { run_id: 44 } }, { marker: { name: 'Gate — Required' } },
  { candidate: { run_number: 41 } }, { candidate: { event: 'workflow_dispatch' } },
  { candidate: { head_branch: 'main' } },
]) {
  test(`keeps the current green grade eligible ${JSON.stringify(options)}`, async () => {
    const f = batchFixture(options);
    await promote(f);
    assert.equal(f.outputs.main, 'rebased');
  });
}

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

const unstableFixture = () => fixture({ existing: true, mergeState: 'unstable' });

for (const [name, alter] of [
  ['a red check of a dispatched run', f => f.checks.push({ name: 'load', status: 'completed', conclusion: 'failure',
    details_url: 'https://github.com/owner/repo/actions/runs/44/job/80' })],
  ['a running check of another workflow', f => f.checks.push({ name: 'kind', status: 'in_progress', conclusion: null,
    details_url: 'https://github.com/owner/repo/actions/runs/45/job/81' })],
  ['a failed commit status', f => f.statuses.push({ context: 'policy', state: 'failure' })],
]) {
  test(`an unstable snapshot merges on its first poll despite ${name} on the triggering commit`, async () => {
    const f = unstableFixture();
    alter(f);
    await promote(f);
    assert.equal(f.now(), 0);
    assert.equal(f.outputs.main, 'rebased');
    assert.deepEqual(f.writes.map(([kind]) => kind), ['merge']);
  });
}

test('an unstable snapshot merges without reading the checks or statuses of the triggering commit', async () => {
  const f = unstableFixture();
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
  assert.equal(f.writes[0][1].sha, 'snapshot');
  assert.ok(f.pages.every(args => args.per_page === 100));
  assert.equal(f.pages.filter(args => args.ref === 'graded').length, 0);
  assert.equal(f.pages.find(args => args.run_id === 42).filter, 'latest');
});

for (const name of ['mutation-stats-seed', 'test-summary']) {
  for (const outcome of ['queued', 'in_progress', 'failure', 'cancelled']) {
    test(`an exempt side job never holds the release: ${name} ${outcome}`, async () => {
      const f = unstableFixture();
      const pending = outcome === 'queued' || outcome === 'in_progress';
      const job = { name, status: pending ? outcome : 'completed', conclusion: pending ? null : outcome };
      f.jobs.push({ id: 75, run_id: 42, ...job });
      f.checks.push(job);
      await promote(f);
      assert.equal(f.now(), 0);
      assert.equal(f.outputs.main, 'rebased');
    });
  }
}

for (const [name, alter] of [
  ['missing current job', f => { f.jobs.length = 0; }],
  ['duplicate current jobs', f => { f.jobs.push({ ...f.jobs[0], id: 73 }); }],
  ['another run job', f => { f.jobs[0].run_id = 41; }],
  ['completed current job', f => { f.jobs[0].status = 'completed'; }],
]) {
  test(`an unverified current grade or job blocks promotion: ${name}`, async () => {
    const f = unstableFixture();
    alter(f);
    await assert.rejects(promote(f));
    assert.deepEqual(f.writes, []);
  });
}

test('finds the exact current job beyond the first hundred jobs', async () => {
  const f = unstableFixture();
  f.jobs.unshift(...Array.from({ length: 110 }, (_, id) => ({ id, run_id: 42, name: `extra-${id}`, status: 'completed', conclusion: 'success' })));
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

function hotfixRun(f, hotfixConclusion = 'success', confirmed = 'success') {
  f.jobs[1].conclusion = 'failure';
  f.jobs.push({ id: 73, run_id: 42, name: 'Launch hotfix grade', status: 'completed', conclusion: hotfixConclusion,
    steps: [{ name: 'Confirm the launch hotfix route', conclusion: confirmed }] },
    { id: 74, run_id: 42, name: 'coverage-baseline', status: 'completed', conclusion: 'failure' });
}

test('promotes a launch hotfix past an unrelated red promotion check', async () => {
  const f = fixture();
  hotfixRun(f);
  await promote(f);
  assert.equal(f.outputs.main, 'rebased');
  assert.ok(f.writes.some(([kind]) => kind === 'merge'));
  assert.equal(f.grades.at(-1).state, 'success');
});

test('never promotes a red grade outside the launch hotfix route', async () => {
  const f = fixture();
  hotfixRun(f, 'success', 'skipped');
  await assert.rejects(promote(f), /Graded run jobs did not pass: Gate — Required failure, coverage-baseline failure$/);
  assert.equal(f.now(), 0);
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
  assert.ok(f.grades.every(grade => grade.state !== 'success'));
});

test('never promotes a launch hotfix whose own grade failed', async () => {
  const f = fixture();
  hotfixRun(f, 'failure');
  await assert.rejects(promote(f),
    /Graded run jobs did not pass: Gate — Required failure, Launch hotfix grade failure, coverage-baseline failure$/);
  assert.equal(f.now(), 0);
  assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
});

test('waits for every launch hotfix run job to finish before promoting', async () => {
  const f = fixture();
  hotfixRun(f);
  f.jobs[3].status = 'in_progress';
  const sleep = f.sleep;
  f.sleep = async ms => {
    assert.ok(f.writes.every(([kind]) => kind !== 'merge'));
    await sleep(ms);
    if (f.now() >= 15000) f.jobs[3].status = 'completed';
  };
  await promote(f);
  assert.equal(f.now(), 15000);
  assert.equal(f.outputs.main, 'rebased');
});

test('promotes a launch hotfix while an exempt side job still runs', async () => {
  const f = fixture();
  hotfixRun(f);
  f.jobs.push({ id: 75, run_id: 42, name: 'test-summary', status: 'in_progress', conclusion: null });
  await promote(f);
  assert.equal(f.now(), 0);
  assert.equal(f.outputs.main, 'rebased');
});
