---
name: create-simple-docs
description: >-
  Write complete documentation for delivered features and logic changes. Use for a
  documentation task, creating project docs, or extending existing feature docs.
argument-hint: "[section-count]"
---

# Create simple docs

1. Read the task, delivered changes and current documentation. Detect the source
   directory from the documentation build configuration, navigation and existing
   pages. Bundle instructions and overlays set the destination and style when
   provided. Otherwise preserve the project's conventions; default to `docs/`.
   Done when the source, audience and feature gaps are known.
2. For a new `docs/` source, use the scripts beside this skill to validate the
   project, create planned pages and generate `00-INDEX.md`. Use numbered domain
   sections with lettered topics (`0A-ARCH.md`, `1A-USAGE.md`); honor a section cap
   when requested. Existing sources keep their layout and navigation. Done when
   every required behavior has a page or a section assigned.
3. Write complete content grounded in current code: purpose, how to use the
   behavior, examples, configuration and operational limits. Explain business
   rules and failure handling where relevant. Update existing pages instead of
   repeating them. Read environment templates for configuration names. Done when
   each identified gap has substantive prose and usable examples.
4. Update navigation and links. Run the project's docs build and link checks in
   CI, or record the missing checker when none exists. Follow the task's review
   and pull request workflow. Done when the documentation checks pass and the
   task's behavior is explained to its intended reader.

Resolve the scripts relative to this SKILL.md in the mounted role home. The
scripts create a new default docs tree; edit an established or overridden source
directly. Keep existing index entries when extending a source.
