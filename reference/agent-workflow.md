# Shared agent workflow

## One project contract

`AGENTS.md` at the repository root is the canonical project instruction file.
Codex discovers it directly. Claude Code discovers `.claude/CLAUDE.md`, whose
`@../AGENTS.md` import loads the same content at session start. The import is
relative to the importing file, not the working directory. Keep that bootstrap
small; edit shared instructions only in `AGENTS.md`.

Topic documents live in the agent-neutral `reference/` directory. Follow the
routing table in `AGENTS.md` and read only the documents needed for the task; do
not import the entire directory into either agent's startup context. Paths in
the project instructions are repository-relative. Bare reference filenames in
the topic documents refer to siblings in `reference/`.

This uses ordinary files and Claude's supported import syntax, without symlinks,
generated copies or machine-specific configuration. Commit `AGENTS.md`, the
Claude bootstrap and `reference/` together so fresh clones have the same setup.

## Maintaining shared knowledge

- Store durable facts, conventions and decisions in `reference/` and update the
  routing table when adding a topic. Private agent memory is not a substitute:
  another agent or a fresh checkout cannot rely on seeing it.
- Keep project rules independent of a particular model or agent tool name.
  Agent-specific settings must not duplicate the shared contract.
- Inspect the current branch and working-tree changes before editing. Preserve
  unrelated work; do not assume a remote-tracking branch is fetched or checked out.
- Keep offline tests separate from explicitly launched live LFS scenarios; see
  `testing.md`. Describe planned tooling as planned until implemented and
  validated. Never infer a successful live run from an offline test result.

## Checking the setup

Start each agent in this repository in a fresh session after changing startup
instructions. Ask Codex to summarize its loaded project instructions. In Claude
Code, use `/context` to inspect Memory files and check that the project bootstrap
is loaded; ask it to identify the shared rules and reference directory.
Both should identify `AGENTS.md` as the source, the EventBus boundary, the real-time
budget, the actuation guards and the same task-specific reference paths.

Filesystem checks confirm the import target and reference paths exist; they do
not replace verification in a fresh Claude/Codex session. Matching instruction
content does not guarantee identical agent behavior.

Official loading semantics:

- [Codex project instructions](https://developers.openai.com/de-DE/docs/agent-configuration/agents-md)
- [Claude Code imports and AGENTS.md](https://code.claude.com/docs/en/memory#agentsmd)
