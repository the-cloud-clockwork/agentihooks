### Fixed

- Plan tracing writes a trace-plan gate log row for every plan it refuses,
  including a missing plan and plan lines the parser rejects, so Doctor rates
  can count those refusals.
