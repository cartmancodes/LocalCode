# One model list across providers: design

**Date:** 2026-10-10. **Branch:** `feat/model-catalog`.

## The problem

`/model` lists only the active provider's models. A bare `/model NAME` always
goes to the active provider; another provider's model needs its prefix
(`/model codex gpt-6-astra`). The cause has three parts:

1. **No list of our own, by design.** Octet keeps no hard-coded catalogue
   (`model.rs`: "Model names remain vendor-owned"). Models come only from
   the running CLI: Claude's initialize reply, or Codex's `model/list`.
2. **Only one CLI runs at a time,** so only one provider's list is known.
   It lives in the connection (`conn.models`), and each new connection
   replaces it, so a provider used minutes ago is forgotten.
3. **So `Selection::parse` cannot tell who owns a name.** A bare name goes
   to the current provider.

The real lists (2026-10-08) are:

- **Claude:** 12 entries, with aliases (`default`, `opus`, `sonnet`, `haiku`,
  `fable`) and full IDs (`claude-opus-5-5`, …).
- **Codex:** 4 (`gpt-6-astra`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`).

Full IDs never collide across providers; `default` is in both. Codex accepts
IDs it does not list: `gpt-5.5` works but is absent from its list.

## Goal

- `/model` shows one list across every provider.
- `/model NAME`, typed or Tab-completed, switches to whichever provider owns
  NAME. As with any cross-provider switch, the conversation goes along.
- The explicit forms (`/model codex X`, `/model claude/X`, `/model claude`)
  keep working.
- Model names are never hard-coded. They are fetched from the providers when
  needed and cached.

## Decisions (agreed)

1. **Discovery:** probe a provider's CLI for its list only when needed, and
   cache the result on disk.
2. **Unlisted names:** resolved by a prefix learned from the fetched lists,
   else the current provider.
3. **Display:** one list in `/model`, plus Tab completion across providers.
4. **Generic:** nothing in the probe, cache, resolution or interface names a
   vendor. A provider supports discovery by implementing one protocol step.

## Behaviour

### Discovery and the cache

- **Cache:** `models.json` in Octet's data directory, next to the journals:
  `--journal-dir` when given, else `$XDG_DATA_HOME/octet/rust-preview`.
  - It holds one entry per provider name: the list (each item's selection,
    full ID, name and description, as `ModelInfo` has today) and when it was
    fetched.
  - It is written atomically (a temporary file, then a rename), mode 0600, so
    parallel windows cannot tear it. A missing, unreadable or malformed file
    is an empty cache, never an error.
- **When a provider is probed:** only if its cached list is missing or older
  than 24 hours.
  - At startup, after the active session is ready, so the two do not compete,
    every other vendor provider whose cache needs it is probed in the
    background.
  - `/model refresh` probes every vendor provider other than the active one
    now.
  - The active provider's live list, which already arrives with every
    connection, updates its cache entry at no cost.
- **A probe** starts the provider's CLI in a catalog-only mode, takes its
  list, and stops it.
  - It writes no journal, opens no vendor session (no Codex thread, no Claude
    turn) and spends no tokens.
  - It gives up after 20 seconds. On failure (not installed, not signed in,
    timeout) the cached list, if any, stays; the failure shows only in
    `/model`.
  - One probe per provider at a time. Headless `--print` and `--rpc` never
    probe.

### Choosing a model

A bare `/model NAME` resolves in this order:

1. **`default`:** always the current provider's default, whatever the lists hold.
2. **An exact match** on a list entry's selection or full ID, in exactly one
   provider's list: that provider. `opus` → Claude, `gpt-6-astra` → Codex.
   A name in several lists stays with the current provider if that is one of
   them; otherwise it is refused with a hint naming the providers and the
   explicit form.
3. **A learned prefix.** Each provider's prefixes are the leading words, up
   to the first `-`, of the full IDs in its fetched list: Codex `gpt`, Claude
   `claude`. A name whose leading word is a prefix of exactly one provider
   goes there: `gpt-5.5` → Codex.
4. **Otherwise the current provider,** as today (custom IDs stay possible).

- **Explicit forms win and are unchanged:** `/model PROVIDER [NAME]`,
  `PROVIDER/NAME`.
- **The notice says where a name was found:** "Model → codex / gpt-6-astra
  (from Codex's list)", or "(by its gpt- prefix)", or nothing for the
  current provider.
- **A pick on another provider** is the ordinary cross-provider switch: a new
  vendor session, with the conversation carried.

### The list

- **`/model` and `/model list [PAGE]`** show the current model's details, then
  one list.
  - The current provider's models come first, then each other provider's in
    table order.
  - Each entry shows the name, `· provider`, the full ID, the description, and
    `Select: /model <selection>`. The provider prefix is added only where the
    plain name would resolve elsewhere: `default`, or a name shared by
    several providers.
- **The header** counts each provider, says how fresh each list is ("live",
  "cached 2 days ago", "probing…", "unavailable: not installed or not signed
  in"), and pages as today (20 per page).
- **The footer** replaces the outdated "Switching providers starts fresh
  context" with: "`/model <name>` switches provider when needed; the
  conversation goes with you. `/model refresh` fetches the lists again."

### Tab completion

- `/model ` followed by a partial name, then Tab, completes from every
  provider's list: selections and full IDs, current provider first.
- **One match** is filled in. **Several** fill their common start and show
  the popup, each item as `name · provider`.
- `/model ` + Tab with nothing typed lists the current provider's entries.

## Components

- **`octet-engine` — a catalog-only protocol step.**
  - A new `Protocol` method opens the CLI only as far as its model list,
    emits `Models`, then `Ready` with an empty session. The default is "not
    supported", so a provider without it is simply never probed.
  - Codex: `initialize`, `initialized`, `model/list` (all pages, the existing
    limits), with no `thread/start`.
  - Claude: `initialize`, whose reply already carries the models.
  - `Config` gains `catalog_only: bool`.
- **`octet_engine::live::probe(engine, binary, cwd) -> Result<Vec<ModelInfo>, String>`.**
  It spawns a catalog-only session, waits for `Ready` (keeping the last
  `Models`), shuts it down, and returns the list. It has a 20 s timeout and
  writes no journal.
- **`octet-core` — `catalog` module.** `Catalogs`, the cache file
  (load/save), `stale(provider, now)`, the learned prefixes, and the lookup
  that `Selection::parse` uses. All of it is pure except load and save.
- **`Selection::parse(input, current, &Catalogs)`.** The resolution order
  above. It returns where the name was found, for the notice.
- **`octet-tui`:**
  - `App` holds the `Catalogs`, kept across reconnects and reloaded from disk
    for a fresh interface. `Event::Models` updates the active provider's
    entry and saves it.
  - `run()` starts the startup probes after the first ready, as a background
    task whose results reach the event loop like vendor events.
  - `/model refresh` asks for the same.
  - `show_models` renders the merged list; the composer's Tab completes model
    names after `/model `.

## Error handling

| Case | What happens |
|---|---|
| Probe fails or times out | The cache stays; `/model` shows "unavailable: …" with the CLI's own message when it gave one |
| Cache file unreadable or malformed | An empty cache; it is rewritten on the next successful fetch |
| Cache write fails | The lists still work for this run; a note says the cache could not be saved |
| Name in several providers' lists | The current provider if it is one of them, else refused with a hint |
| A probe still running at quit | Stopped with the session's usual shutdown (no orphan CLI) |

## Testing

- **Core (unit):**
  - resolution: exact match per provider, `default`, ambiguity both ways,
    learned prefixes (`gpt-5.5` → Codex), unknown names → current, explicit
    forms unchanged;
  - prefixes learned from lists;
  - staleness at 24 h;
  - cache round-trip, malformed file → empty, mode 0600.
- **Engine (fake vendors):**
  - the Codex probe returns every page of the list and never sends
    `thread/start`;
  - the Claude probe sends no prompt;
  - a CLI that never answers times out after 20 s (a shortened limit in
    tests);
  - no journal is written.
- **TUI:**
  - the merged list (order, provider labels, prefixes only where needed,
    freshness);
  - a pick on another provider makes a cross-provider plan with the
    conversation carried;
  - Tab completion across providers;
  - `Event::Models` updates and saves the cache.
- **Live (real CLIs):**
  - probe time per CLI;
  - Codex's session history gains no entry from a probe;
  - from Claude, `/model gpt-6-astra` switches to Codex, and `/model opus`
    switches back, with the conversation recalled;
  - a second launch within 24 h starts no extra CLI.

## Out of scope

- An arrow-key model picker.
