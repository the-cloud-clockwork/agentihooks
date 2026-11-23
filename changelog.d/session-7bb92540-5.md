### Fixed

- Placement skips a hive that reports a release older than the first release of the role it would host: an expert, first hosted by 2.17.1181, is never put on an older hive, pinned or not. A pre-release of that release counts as older. A hive that reports no release, a text that is no version, or a development or local build, is not judged.
