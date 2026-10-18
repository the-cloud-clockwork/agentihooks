### Fixed

- An agent's status comment on a ledger item amends its latest comment only
  when that comment still ends the thread. After any later comment, by the
  master, another agent or the operator, it posts a new comment at the end, so
  a fresh block notice is never hidden above later comments.
