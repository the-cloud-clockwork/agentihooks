### Changed

- The swarm guide's ready merge pass queue exit section describes how the
  merge pass judges a pull request's own merge group runs, including a
  cancelled run with a failed job, the requeue count, the commands that list
  those runs and their jobs, and the `swarm merge queue` refusal of a head
  whose last merge group failed.
