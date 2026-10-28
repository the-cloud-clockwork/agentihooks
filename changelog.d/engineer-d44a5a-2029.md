### Fixed

- The hive daemon starts an ended headroom or herdr stats thread again at the next beat, so one failed timer wait no longer leaves a hive publishing headroom only at its beats until its Redis connection drops. A lost wait now prints `hive headroom wait failed` or `hive herdr stats wait failed` to stderr.
