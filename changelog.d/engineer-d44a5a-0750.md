### Fixed

- The Doctor trace step reads wait history only for agents with a turn gap over the limit, inside a 15 second wait budget, capped by the 55 second step deadline, and reports the agents left unread as a partial read instead of overrunning the tick deadline.
