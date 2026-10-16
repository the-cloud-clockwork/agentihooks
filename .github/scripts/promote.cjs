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

module.exports = async ({ github, context, core, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) }) => {
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
    const promoted = /^Graded dev revision: ([0-9a-f]{40})$/m.exec(main.commit.message)?.[1];
    if (promoted && ['behind', 'identical'].includes((await github.rest.repos.compareCommits({ ...api, base: promoted, head: context.sha })).data.status)) {
      throw new Error('Graded revision is not newer than the last promotion');
    }
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
    for (let attempt = 0; attempt < 48; attempt++) {
      const latest = (await github.rest.pulls.get({ ...api, pull_number: pull.number })).data;
      if (latest.head.sha !== snapshot) throw new Error('Promotion pull request moved away from the graded head');
      if (latest.mergeable === false) throw new Error('Promotion pull request has merge conflicts');
      if (latest.mergeable === true && (latest.mergeable_state === 'clean' ||
          latest.mergeable_state === 'unstable' && await gradeIsComplete(github, context, api))) {
        ready = true;
        break;
      }
      await sleep(5000);
    }
    if (!ready) throw new Error('Promotion checks did not become mergeable within four minutes');
    const merged = (await github.rest.pulls.merge({ ...api, pull_number: pull.number, sha: snapshot, merge_method: 'squash', commit_message: body })).data;
    if (!merged.merged) throw new Error('GitHub refused the promotion merge');
    main = (await github.rest.repos.getCommit({ ...api, ref: merged.sha })).data;
    if (main.commit.tree.sha !== graded.commit.tree.sha) throw new Error('Promoted tree differs from the graded dev tree');
    try {
      await github.rest.git.deleteRef({ ...api, ref });
    } catch (error) {
      if (error.status !== 404 && !(error.status === 422 && error.message.includes('Reference does not exist'))) throw error;
    }
    core.setOutput('pull-request', pull.html_url);
  }
  core.setOutput('graded', context.sha);
  core.setOutput('main', main.sha);
  await core.summary.addHeading('Promotion').addTable([
    ['Graded dev', context.sha], ['Promoted main', main.sha], ['Tree', graded.commit.tree.sha],
  ]).write();
};
