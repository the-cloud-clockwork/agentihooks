### Fixed

- A tick step still running past its budget from an earlier pass is held instead of started again, so placement can no longer spawn twice across overlapping passes; the engine records the step as held.
