async function gradeIsComplete(github, context, api) {
  const [jobs, checks, statuses] = await Promise.all([
    github.paginate(github.rest.actions.listJobsForWorkflowRun, { ...api, run_id: context.runId, filter: 'latest', per_page: 100 }),
    github.paginate(github.rest.checks.listForRef, { ...api, ref: context.sha, filter: 'latest', per_page: 100 }),
    github.paginate(github.rest.repos.listCommitStatusesForRef, { ...api, ref: context.sha, per_page: 100 }),
  ]);
  const current = jobs.filter(job => job.run_id === context.runId && job.name === 'promote / promote' && job.status === 'in_progress');
  if (current.length !== 1) return false;
  const runUrl = `${context.serverUrl}/${api.owner}/${api.repo}/actions/runs/${context.runId}/job/`;
  const own = check => check.url === current[0].check_run_url && check.details_url === `${runUrl}${current[0].id}` &&
    check.name === current[0].name && check.status === 'in_progress' && check.conclusion === null;
  const latest = new Map();
  for (const status of statuses) {
    if (!latest.has(status.context)) latest.set(status.context, status.state);
  }
  return checks.filter(own).length === 1 &&
    checks.some(check => check.name === 'Gate — Required' && check.status === 'completed' &&
      check.conclusion === 'success' && check.details_url.startsWith(runUrl)) &&
    checks.every(check => own(check) || check.status === 'completed' && ['success', 'skipped'].includes(check.conclusion)) &&
    [...latest.values()].every(state => state === 'success');
}

async function snapshotChecksComplete(github, api, snapshot, number) {
  let cursor = null;
  let complete = true;
  do {
    const result = await github.graphql(`query($owner: String!, $repo: String!, $snapshot: GitObjectID!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        object(oid: $snapshot) { ... on Commit { statusCheckRollup {
          contexts(first: 100, after: $cursor) {
            nodes {
              ... on CheckRun { name status conclusion isRequired(pullRequestNumber: $number) }
              ... on StatusContext { context state isRequired(pullRequestNumber: $number) }
            }
            pageInfo { hasNextPage endCursor }
          }
        } } }
      }
    }`, { ...api, snapshot, number, cursor });
    const contexts = result.repository.object.statusCheckRollup?.contexts;
    for (const check of contexts?.nodes || []) {
      if (!check.isRequired) continue;
      const pending = check.context ? ['EXPECTED', 'PENDING'].includes(check.state) : check.status !== 'COMPLETED';
      const passed = check.context ? check.state === 'SUCCESS' : ['SUCCESS', 'SKIPPED', 'NEUTRAL'].includes(check.conclusion);
      if (!pending && !passed) throw new Error(`Required snapshot check failed: ${check.context || check.name}`);
      if (pending) complete = false;
    }
    cursor = contexts?.pageInfo.hasNextPage ? contexts.pageInfo.endCursor : null;
  } while (cursor);
  return complete;
}

async function deleteSnapshot(github, api, ref) {
  try {
    await github.rest.git.deleteRef({ ...api, ref });
  } catch (error) {
    if (error.status !== 404 && !(error.status === 422 && error.message.includes('Reference does not exist'))) throw error;
  }
}

async function supersededBy(github, api, graded, main) {
  const promoted = /^Graded dev revision: ([0-9a-f]{40})$/m.exec(main.commit.message)?.[1];
  if (!promoted || (await github.rest.repos.compareCommits({ ...api, base: promoted, head: graded })).data.status === 'ahead') return null;
  return promoted;
}

async function endSuperseded(core, graded, main, promoted) {
  core.setOutput('graded', graded);
  core.setOutput('superseded', promoted);
  await core.summary.addHeading('Promotion superseded').addTable([
    ['Graded dev', graded], ['Main', main.sha], ['Main already promoted dev', promoted],
  ]).write();
}

module.exports = async ({ github, context, core, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), now = Date.now }) => {
  const { owner, repo } = context.repo;
  const repository = `${owner}/${repo}`;
  if (context.eventName !== 'push' || context.ref !== 'refs/heads/dev' ||
      context.payload.repository.full_name !== repository) return;
  const api = { owner, repo };
  const graded = (await github.rest.repos.getCommit({ ...api, ref: context.sha })).data;
  let main = (await github.rest.repos.getCommit({ ...api, ref: 'main' })).data;
  if (main.commit.tree.sha !== graded.commit.tree.sha) {
    const head = `promotion/${context.sha}`;
    const ref = `heads/${head}`;
    const pulls = (await github.rest.pulls.list({ ...api, state: 'open', base: 'main', head: `${owner}:${head}` })).data;
    let promoted = await supersededBy(github, api, context.sha, main);
    if (promoted) return endSuperseded(core, context.sha, main, promoted);
    const body = `Graded dev revision: ${context.sha}\n\nTests: ${context.serverUrl}/${repository}/actions/runs/${context.runId}`;
    let snapshot;
    try {
      snapshot = (await github.rest.git.getRef({ ...api, ref })).data.object.sha;
    } catch (error) {
      if (error.status !== 404 || pulls.length) throw error;
      snapshot = (await github.rest.git.createCommit({
        ...api, message: `Promote green dev to main\n\n${body}`, tree: graded.commit.tree.sha, parents: [main.sha],
      })).data.sha;
      await github.rest.git.createRef({ ...api, ref: `refs/${ref}`, sha: snapshot });
    }
    const staged = (await github.rest.git.getCommit({ ...api, commit_sha: snapshot })).data;
    if (staged.tree.sha !== graded.commit.tree.sha || staged.parents.length !== 1 || staged.parents[0].sha !== main.sha) {
      throw new Error('Promotion snapshot is not the graded tree on main');
    }
    const pull = pulls[0] || (await github.rest.pulls.create({
      ...api, base: 'main', head, title: 'Promote green dev to main', body,
    })).data;
    if (pull.head.sha !== snapshot || pull.head.repo.full_name !== repository || pull.base.ref !== 'main') {
      throw new Error('Promotion pull request does not match the snapshot commit');
    }
    let ready = false;
    const deadline = now() + 30 * 60 * 1000;
    while (now() < deadline) {
      const latest = (await github.rest.pulls.get({ ...api, pull_number: pull.number })).data;
      if (latest.head.sha !== snapshot) throw new Error('Promotion pull request moved away from the graded head');
      if (latest.mergeable === false) throw new Error('Promotion pull request has merge conflicts');
      const complete = await snapshotChecksComplete(github, api, snapshot, pull.number);
      if (complete && latest.mergeable === true && (latest.mergeable_state === 'clean' ||
          latest.mergeable_state === 'unstable' && await gradeIsComplete(github, context, api))) {
        ready = true;
        break;
      }
      await sleep(Math.min(5000, Math.max(0, deadline - now())));
    }
    if (!ready) throw new Error('Promotion checks did not become mergeable within thirty minutes');
    const current = (await github.rest.repos.getCommit({ ...api, ref: 'main' })).data;
    if (current.sha !== main.sha) {
      promoted = await supersededBy(github, api, context.sha, current);
      if (promoted) {
        await github.rest.pulls.update({ ...api, pull_number: pull.number, state: 'closed' });
        await deleteSnapshot(github, api, ref);
        return endSuperseded(core, context.sha, current, promoted);
      }
      throw new Error('Promotion snapshot is stale, main moved during the wait');
    }
    const merged = (await github.rest.pulls.merge({ ...api, pull_number: pull.number, sha: snapshot, merge_method: 'squash', commit_message: body })).data;
    if (!merged.merged) throw new Error('GitHub refused the promotion merge');
    main = (await github.rest.repos.getCommit({ ...api, ref: merged.sha })).data;
    if (main.commit.tree.sha !== graded.commit.tree.sha) throw new Error('Promoted tree differs from the graded dev tree');
    await deleteSnapshot(github, api, ref);
    core.setOutput('pull-request', pull.html_url);
  }
  core.setOutput('graded', context.sha);
  core.setOutput('main', main.sha);
  await core.summary.addHeading('Promotion').addTable([
    ['Graded dev', context.sha], ['Promoted main', main.sha], ['Tree', graded.commit.tree.sha],
  ]).write();
};

module.exports.closeAbandoned = async ({ github, context, core, app, deadline = Infinity }) => {
  const { owner, repo } = context.repo;
  const repository = `${owner}/${repo}`;
  if (context.eventName !== 'push' || context.ref !== 'refs/heads/dev' ||
      context.payload.repository.full_name !== repository) return;
  if (!app) throw new Error('The promotion App slug is required to recognise its own pull requests');
  const api = { owner, repo };
  const run = `${context.serverUrl}/${repository}/actions/runs/${context.runId}`;
  const outcomes = [];
  const failures = [];
  const open = await github.paginate(github.rest.pulls.list, { ...api, state: 'open', base: 'main', per_page: 100 });
  for (const [index, pull] of open.entries()) {
    if (Date.now() > deadline) {
      outcomes.push([`${open.length - index} not examined`, '', 'left for the next run, the cleanup time budget ran out']);
      break;
    }
    const graded = /^promotion\/([0-9a-f]{40})$/.exec(pull.head.ref)?.[1];
    if (!graded || graded === context.sha || pull.user?.login !== `${app}[bot]` ||
        pull.head.repo?.full_name !== repository || pull.base.ref !== 'main') continue;
    try {
      if ((await github.rest.repos.compareCommits({ ...api, base: graded, head: context.sha })).data.status !== 'ahead') continue;
      await github.rest.issues.createComment({ ...api, issue_number: pull.number,
        body: `Abandoned: graded dev revision ${context.sha} supersedes ${graded}. Closed by the promotion run ${run}.` });
      await github.rest.pulls.update({ ...api, pull_number: pull.number, state: 'closed' });
      const ref = `heads/${pull.head.ref}`;
      let snapshot = 'deleted';
      try {
        if ((await github.rest.git.getRef({ ...api, ref })).data.object.sha === pull.head.sha) await deleteSnapshot(github, api, ref);
        else snapshot = 'kept, it moved';
      } catch (error) {
        if (error.status !== 404) throw error;
        snapshot = 'already gone';
      }
      outcomes.push([`#${pull.number}`, graded, `closed, snapshot ${snapshot}`]);
    } catch (error) {
      failures.push(`#${pull.number}: ${error.message}`);
      outcomes.push([`#${pull.number}`, graded, `failed: ${error.message}`]);
    }
  }
  await core.summary.addHeading('Abandoned promotion pull requests')
    .addTable([['Pull request', 'Graded dev', 'Outcome'], ...outcomes]).write();
  if (failures.length) throw new Error(`Abandoned promotion cleanup failed: ${failures.join('; ')}`);
};
