### Changed

- The tick lists a repository's open pull requests for the fleet red hold at most once every ten minutes. The fleet red notice step has a measured budget of thirty seconds, so its listing save is kept instead of dropped as late, and the claim filter that every ready task list builds reads that saved listing before it lists live.
