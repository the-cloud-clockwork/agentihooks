### Fixed

- Trace export resumes a paged transcript just past the last paged record by its stored byte position, so a repeated metadata record no longer sends the resume back to an earlier copy and paged spans are never counted twice.
