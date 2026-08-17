# Optional provider protocol

The default runtime is the standalone SQLite implementation. Providers are
optional process boundaries, not dependencies of the public package.

An implementation exposes JSON operations with the same names as the CLI:

```text
doctor  sync  search  get  timeline  feedback  promote
```

Provider responses should preserve the source result and include:

```json
{
  "schema_version": 1,
  "provider": "example",
  "operation": "search",
  "canonical_repo_changed": false
}
```

`provider_protocol.py` contains the normalization and manifest helpers. It
does not load SDKs, start services, download models, or handle credentials.
Git path/commit/line citation remains authoritative; an external provider may
return candidates, but the core must validate them before marking them
verified. TencentDB, MemOS, Cognee, Mem0, MemPalace and Hindsight can be
implemented behind this seam, but none is required to run the public release.
