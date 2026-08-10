# Automatic capture and memory sedimentation

The repository-memory Skill is not itself a conversation hook. MCP is a
request/response protocol and cannot observe an agent lifecycle unless the host
registers an extension. When a host provides an `agent_end`-style lifecycle
event, the optional adapter can connect that event to the same runtime used by
the CLI and MCP.

## Contract

```text
successful agent turn
        |
        v
bounded user/assistant payload
        |
        v
redaction + normalization + idempotency
        |
        v
L0 durable conversation --read-back--> verified
        |
        +-- asynchronous observation --> L1 pending | verified
        |
        +-- durable/decision-like turn --> L2 candidate, evidence=pending
                                      |
                                      +-- explicit review/accept
                                                |
                                                v
                                      L3 profile/core --read-back--> accepted
```

The hook is deliberately conservative. It does not capture system prompts,
developer instructions, tool outputs, or function arguments. It bounds the
number and size of messages, redacts common credential forms, and uses a
stable run/session key so a retry cannot duplicate a memory. A short casual
answer can be ignored; a decision, preference, completed change, configuration,
plan, blocker, or sufficiently substantive answer can produce an L2 candidate.

## What the agent is allowed to claim

The callback result is a receipt of a write attempt and verification, not a
quality judgment:

| State | Meaning | Can be used as a fact? |
| --- | --- | --- |
| `L0=verified` | The durable raw conversation was written and read back by id. | Only as “the conversation recorded ...” |
| `L1=pending` | Atomic extraction has not been observed yet. | No |
| `L1=verified` | An atomic record was observed after this turn. | Only with its returned evidence/status |
| `L2=candidate` | A derived summary exists in a pending review store or native scenario. | No; inspect first |
| `L3=accepted` | An explicit promotion wrote the profile/core and read it back. | Yes, subject to the returned citation and scope |

`ready` or `reachable` only describes transport and API capability. It does
not mean the memory database contains useful L2/L3 content.

## Explicit review and promotion

For a candidate returned by the memory runtime, first call `memory_get` or the
CLI equivalent and check its user/assistant evidence, L0 IDs, L1 status, and
`evidence_status`. Promotion is a separate write action and must include an
explicit acceptance flag. The runtime then:

1. reads the pending candidate;
2. changes its state to accepted in the derived L3 content;
3. writes the native core/profile;
4. reads the profile back and verifies the accepted content;
5. archives the pending candidate outside the pending search tree.

If any step fails, leave the candidate pending and report the failure. Do not
edit the canonical Git repository from this path.

## Host integration checklist

After installing a lifecycle adapter, verify all of these with a synthetic
turn in an isolated identity/session:

1. the host registers the hook and the callback completes;
2. a real `L0=verified` receipt is returned;
3. L1 is reported `pending` or `verified`, never invented;
4. an L2 candidate is returned only for a durable turn;
5. a duplicate event does not create a second candidate;
6. a fabricated search does not return the candidate as `verified`;
7. explicit acceptance produces an L3 read-back;
8. the canonical repository remains unchanged.

The same checks should be repeated for each host/profile that opts in. A Skill
installation alone does not prove that a host lifecycle hook is registered.
