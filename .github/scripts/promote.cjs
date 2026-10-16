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
    if (!pulls.length) {
      try {
        const snapshot = (await github.rest.git.getRef({ ...api, ref })).data;
        if (snapshot.object.sha !== context.sha) throw new Error('Promotion snapshot differs from the graded revision');
      } catch (error) {
        if (error.status !== 404) throw error;
        await github.rest.git.createRef({ ...api, ref: `refs/${ref}`, sha: context.sha });
      }
    }
    const pull = pulls[0] || (await github.rest.pulls.create({
      ...api, base: 'main', head, title: 'Promote green dev to main',
      body: `Graded dev revision: ${context.sha}\n\nTests: ${context.serverUrl}/${repository}/actions/runs/${context.runId}`,
    })).data;
    if (pull.head.sha !== context.sha || pull.head.repo.full_name !== repository || pull.base.ref !== 'main') {
      throw new Error('Promotion pull request does not match the graded dev head');
    }
    let ready = false;
    for (let attempt = 0; attempt < 48; attempt++) {
      const latest = (await github.rest.pulls.get({ ...api, pull_number: pull.number })).data;
      if (latest.head.sha !== context.sha) throw new Error('Promotion pull request moved away from the graded head');
      if (latest.mergeable === false) throw new Error('Promotion pull request has merge conflicts');
      if (latest.mergeable === true && latest.mergeable_state === 'clean') {
        ready = true;
        break;
      }
      await sleep(5000);
    }
    if (!ready) throw new Error('Promotion checks did not become mergeable within four minutes');
    const merged = (await github.rest.pulls.merge({ ...api, pull_number: pull.number, sha: context.sha, merge_method: 'squash' })).data;
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
