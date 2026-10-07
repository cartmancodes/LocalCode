# The protocol gate

`protocol-gate` runs one scenario against a real vendor CLI (`claude` or
`codex`) and prints what happened as JSON evidence. It is how Octet checks
that a vendor's wire protocol still behaves the way the drivers expect,
before a CLI update reaches users. It launches the CLI with Octet's own
arguments and process limits (`octet_engine::live::launch_args` and
`vendor_process`), adding only what a scenario needs (Claude's `--tools`,
and the fixture MCP server), so it checks the contract Octet ships.

It talks to the real vendor, so each run uses your login and your quota.
Keep runs few and small.

## Run it

```sh
make gate ENGINE=claude SCENARIO=simple
```

or directly, with every option:

```sh
scripts/rust-env.sh cargo run --locked -p octet-gate --bin protocol-gate -- \
  --engine claude --scenario simple \
  [--binary PATH] [--workdir NEW_DIR] [--output FILE] [--timeout SECONDS]
```

- **`--binary`:** the CLI to run; the engine's name on `PATH` by default.
- **`--workdir`:** a fixture workspace to create, and keep; by default a
  temporary one, removed when the run ends, whatever its outcome. It must
  not exist yet.
- **`--output`:** also write the evidence to this file.
- **`--timeout`:** 1–300 seconds for the scenario.

## Scenarios

| Scenario | Checks | Claude | Codex |
| --- | --- | --- | --- |
| `initialize` | The handshake | yes | yes |
| `simple` | One turn to completion | yes | yes |
| `interrupt` | Cancelling a running turn | yes | yes |
| `approval-allow` | An approval allowed; the fixture file is written | yes | yes |
| `approval-deny` | An approval denied | yes | yes |
| `approval-interrupt` | Cancelling while an approval waits | yes | yes |
| `resume` | Reopening a session | yes | yes |
| `fork` | Forking a session | yes | yes |
| `compact` | Compacting a session's context | yes | yes |
| `mcp` | An MCP tool call | yes | no |
| `hook` | A hook callback | yes | no |
| `user-input` | A request for user input | no | yes |
| `tool-only` | A turn that only runs a tool | no | yes |
| `failure` | A turn the vendor fails | no | yes |

## Result

The evidence goes to stdout as one JSON object; its `status` starts with
`passed` when the vendor behaved as the drivers expect.

| Exit | Meaning |
| --- | --- |
| 0 | The scenario passed |
| 1 | The vendor did something else; the evidence says what |
| 2 | A usage error, or the fixture workspace could not be set up |

## Offline tests

The gate's own logic is tested without a vendor: `crates/octet-gate/tests`
runs the gate binary against scripted wire transcripts (`protocol_gate.rs`)
and checks the contract fixtures (`contracts.rs`). These run in
`make rust-check`; the live scenarios above do not.
