# ruleid: open-listing-with-rollups
PAGED = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 25, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        url
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name conclusion startedAt completedAt }
        } } } } } }
      }
    }
  }
}"""

# ruleid: open-listing-with-rollups
FILTERED = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {
  pullRequests(states: [OPEN], baseRefName: "dev", first: 50) {
    nodes { url commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { totalCount } } } } } }
  }
} }"""

# ok: open-listing-with-rollups
NUMBERS = """query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { number }
    }
  }
}"""

# ok: open-listing-with-rollups
ROLLUP = """url
      commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
        __typename
        ... on CheckRun { name conclusion startedAt completedAt }
      } } } } } }"""

# ok: open-listing-with-rollups
STATES = """query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) {
  pullRequests(states: OPEN, first: 100) {
    nodes { number commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } }
  }
} }"""
