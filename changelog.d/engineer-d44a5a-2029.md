### Fixed

- The hive daemon starts an ended headroom or herdr stats thread again at the next beat, so a hive no longer reports `headroom=missing` between beats for good after one failed timer wait. A lost wait now prints `hive headroom wait failed` or `hive herdr stats wait failed` to stderr.
