### Changed

- GitHub reads made through the App credential exec path (the worker `gh`
  shim and hive agent launches) are refused while the installation's core
  budget is below `AGENTIHOOKS_GITHUB_READ_RESERVE` (default 1000), naming the
  reset time. Writes, merges and queueing always run.
