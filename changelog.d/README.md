# Changelog fragments

Every pull request records its changelog entry in its own file here, named from
its branch: `changelog.d/<branch>.md`. One file per pull request means parallel
pull requests never edit the same lines, so they merge without conflict.

Use the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) section
headings, and only the ones the change needs:

```markdown
### Added

- What a user can do now that they could not before.

### Fixed

- The behaviour that was wrong and is now right.
```

At release, `scripts/ci_release.sh notes` collects every fragment added or
changed since the previous stable tag, groups their entries under one heading
per section and prepends them to the release notes of the promotion and the
manual release workflow. Entries released before fragments existed stay in
`CHANGELOG.md`.
