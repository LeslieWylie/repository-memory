# Plugin architecture

`repository-memory` is now a plugin-shaped runtime without making any vendor
plugin a required dependency.

```text
Host (OpenClaw / Claude / Codex)
        |
        +-- Skill: instructions and evidence policy
        +-- MCP: memory_doctor / sync / search / get / timeline / observe / reflect
        +-- OpenClaw lifecycle extension: recall + capture + audit
        |
        +-- Python runtime
              +-- repository provider: Git snapshot + citation index
              +-- memory provider: standalone SQLite L0-L3
              +-- optional providers: TencentDB / Memmy / configured adapter
              +-- optional semantic provider: explicitly configured HF model
```

## Provider boundary

Every provider is discovered at runtime and must return the same normalized
fields: `id`, `layer`, `status`, `content`/`snippet`, provenance, freshness,
and read-back state. Providers cannot change the canonical source or silently
replace repository evidence with conversation memory.

The default provider is the standalone SQLite runtime. The OpenClaw extension
does not own a database: it invokes the same Python runtime used by the CLI
and MCP. This prevents the common failure mode where the bot talks to one
memory store while the CLI reports another.

## Reused lifecycle ideas

The lifecycle follows the useful common denominator seen in MemOS and
TencentDB:

```text
L0 conversation/trace
    -> L1 atomic/trace
    -> L2 policy/scenario candidate
    -> explicit review
    -> L3 world-model/profile accepted by explicit promotion
```

`memory_timeline` is diagnostic only. It does not promote a trace, and it
does not turn an L0/L1 conversation into Git evidence. `memory_search` keeps
the repository, local memory, and `scope=all` groups separate.
`memory_observe` is an ordered raw trace; `memory_reflect` is a generated,
candidate-labelled digest and never an accepted fact.

## OpenClaw installation

Install only the repository-memory extension for the selected agent. Do not
install the MemOS, TencentDB, Memmy, or another provider's OpenClaw memory
slot alongside it unless a deliberate compatibility experiment explicitly
changes the memory slot and has its own rollback plan.

The extension is advisory by default. It records routing/capture observations
but does not block ordinary `exec`, `read`, Git, test, or debugging work.
