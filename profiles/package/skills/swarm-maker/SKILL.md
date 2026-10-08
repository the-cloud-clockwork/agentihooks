---
name: swarm-maker
description: Guides users to build bundle overlays and purpose built swarms while keeping the five base roles. Interviews for capabilities, scaffolds a missing bundle, creates and validates overlays, and selects them through shared swarm data. Use when a user asks for custom profiles, a custom swarm, domain experts, or a purpose built swarm; use init-swarm for an already accepted plan that only needs launching.
---

# Swarm Maker

Requires Git and the installed `agentihooks` CLI. If agentihooks is missing,
install the package with `uv tool install agentihooks`; a missing Git requires
the host's system dependency process. Run commands below; do not reimplement
the scaffold or edit the ledger JSON or swarm store directly.

1. **Interview.** Reuse answers already given. Ask for the outcome and its proof,
   an existing swarm or a new accepted plan, what each role needs to know or do,
   source material to reuse, and the required tools and harnesses. Summarize a
   role → overlays → capabilities → proof mapping. Keep `master`, `engineer`,
   `planner`, `qa` and `cicd` as the five base roles; domain experts are overlays,
   not new roles. Each agent can wear at most three selected overlays. If the
   request exceeds that cap, state the three-overlay limit and ask which to
   omit before scaffolding or saving a selection.
   Done when the mapping has a checkable outcome and no unanswered choice that
   changes its capabilities.

2. **Locate the bundle.** Run `agentihooks bundle list`. Reuse the linked bundle
   when present. When none is linked, agree on a new or empty bundle directory
   and run:

   ```bash
   agentihooks bundle new BUNDLE_DIR
   agentihooks bundle list
   ```

   `bundle new` creates the layout, initializes Git and links it; a nonempty
   directory is refused. Keep overlays in this versioned bundle, never in repo
   local profiles. Follow the bundle's development workflow when changing an
   existing bundle. Done when `bundle list` reports the intended bundle and its
   `profiles` directory is available in the authorized workspace.

3. **Build the overlays.** Inspect existing bundle profiles before creating
   another. Reuse a matching overlay; preserve existing content when editing.
   For each new overlay run:

   ```bash
   agentihooks overlay new OVERLAY_NAME \
     --wears engineer,qa
   ```

   Replace the example roles with the mapping's roles. The command writes
   `profiles/OVERLAY_NAME/profile.yml` with `kind: overlay` and `wears`, a
   `CLAUDE.md`, `.claude/.mcp.json`, and skills and rules directories. Keep the
   manifest without `extends`. Write the domain persona in `CLAUDE.md` and
   reusable rules and skills in the overlay's `.claude` directories; copy only
   the requested capabilities from source material. Use skill-authoring for
   any new skill. Add tools through the harness's native settings and MCP
   format when that harness is requested. Use environment variable references
   for credentials. Done when every mapped capability has its intended asset
   or tool configuration and every overlay declares the roles it can wear.

4. **Validate and version.** Run for every created or edited overlay:

   ```bash
   agentihooks overlay check OVERLAY_NAME
   ```

   Fix every reported fault and repeat until each exits zero. This validates
   the profile structure; also run the relevant asset loader or domain probe
   from the agreed proof. Commit and publish the bundle changes through its
   repository workflow before selection. A new bundle needs its Git remote
   and branch workflow established before publishing; respect protected
   branches. Swarm v2 hives must fetch the same committed bundle revision;
   local uncommitted files cannot supply it. Done when all checks pass and the
   overlays exist at a bundle revision accessible to the intended workers.

5. **Select through shared data.** For an existing swarm, set a role default:

   ```bash
   agentihooks swarm SWARM set \
     overlays-engineer=tuner,trader
   ```

   Use the agreed names and one of the five base roles in `overlays-ROLE`.
   For a task override use the authorized ledger task command:

   ```bash
   agentihooks ledger \
     --slug SWARM \
     --as AGENT \
     task set TASK \
     overlays=risk-auditor
   ```

   Explain that the task's list replaces the role default. `overlays=` is an
   explicit empty task override; `overlays-engineer=` clears the role default. An omitted task
   field inherits the role default. Every selected overlay must wear the
   agent's role. The page's OVERLAYS controls set the same role defaults.
   Explain all three launch cases: selections apply at the next fresh launch;
   running agents keep their current overlays; relaunches and handoffs retain
   their recorded overlays. For a new swarm,
   use init-swarm only after the plan is accepted, then set the role defaults
   before its start step. Let that skill generate the swarm name and tasks.
   Done when `agentihooks swarm SWARM status` and the affected task record show
   the intended saved selections, or a new swarm's selection mapping is ready
   for its accepted plan without claiming it has launched.

6. **Verify the result.** Read the next fresh agent's status and launch check:
   the chosen overlays must appear in the agent record and render stamp and
   pass the launch check. Names plus the bundle revision belong in the launch
   record; shared swarm configuration or task data owns the selection. A remote
   hive renders from that revision, not workstation files. Report the bundle
   revision, role mapping, validation results and selection commands. If no
   launch was requested or available, name launch verification as outstanding.
   Done when the agreed proof passes, or the delivered overlays and the exact
   remaining launch proof are distinguished.
