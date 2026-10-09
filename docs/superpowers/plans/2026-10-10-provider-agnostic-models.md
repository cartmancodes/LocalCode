# One model list across providers: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/model` lists every provider's models, fetched from the
providers' own CLIs when needed and cached. A bare `/model NAME` switches
to whichever provider owns NAME.

**Architecture:**

- **Engine:** a catalog-only mode opens a CLI just far enough to list its
  models (Codex: no `thread/start`; Claude: initialize only). `probe()`
  wraps that mode.
- **`octet-core`:** a `catalog` module holds the per-provider lists, caches
  them in `models.json`, and answers who owns a name.
  `Selection::resolve` uses it.
- **TUI:** a `models` module owns the cache, the background probes and the
  freshness labels. `/model` renders one merged list, and Tab completes
  model names.

**Tech stack:** Rust 1.98.1, edition 2024, tokio, serde_json, ratatui. No new
dependencies.

**Spec:** `docs/superpowers/specs/2026-10-10-provider-agnostic-models-design.md`

## Global constraints

- **Running Cargo:**
  - always through `scripts/rust-env.sh`;
  - the gate is `make rust-check` (fmt, clippy pedantic `-D warnings`,
    `cargo doc -D warnings`, tests);
  - the gate must pass after every task.
- **TDD:** every new test is watched failing before its implementation.
- **Lint exceptions:** `#[expect(lint, reason = "…")]`; never a bare `#[allow]`.
- **Line length:** product lines are at most 120 columns.
- **Commits:** every commit message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage by path.
- **No hard-coded vendor names or model names** in the probe, cache,
  resolution or interface code. Iterate `Engine::ALL`, filtered by
  `is_vendor()`, and read the provider row.
- **Probe timeout:** `octet_engine::live::PROBE_LIMIT` = 20 s.
- **Staleness:** `octet_core::catalog::STALE_AFTER` = 24 h.
- **Cache file:** `models.json` (`octet_core::catalog::FILE`) in the journal
  directory (`run()`'s `directory`), mode 0600, written atomically.
- **Real vendors:** tiny prompts. For Codex use `--model gpt-5.5`, or a model
  from its list. Never edit the user's Codex or Claude configuration.

## Review focus

1. **Two windows saving `models.json` at once** must leave a file that parses.
   Each writes a temporary file and renames it (Task 2 test).
2. **A hand-edited or old `models.json`** with wrong types, unknown
   providers, control characters or over-long names is skipped entry by
   entry, never trusted (Task 2 test).
3. **A CLI that never answers the probe** (a login prompt, a hang) gives up at
   the limit and leaves no process behind (Task 1 test).
4. **A Codex probe must never create a thread,** on every path including a
   `model/list` error (Task 1 tests).
5. **`/model refresh` while a probe runs** must not start a second probe for
   the same provider. A failed provider is not re-probed on every reconnect
   (Task 4 tests).

---

### Task 1: Engine — a catalog-only mode and `probe()`

**Files:**

- Modify:
  - `crates/octet-engine/src/live/mod.rs`: `Config.catalog_only`, the
    `Provider.lists_models` field, `PROBE_LIMIT`, `probe()`
  - `crates/octet-engine/src/live/codex.rs`: `handshaken`, `catalog_page`, the
    row
  - `crates/octet-engine/src/live/claude.rs`, `demo.rs`: the row
  - every `Config { … }` literal: `crates/octet/src/args.rs:79`,
    `crates/octet-core/src/model.rs` (3), `crates/octet-core/tests/session.rs`,
    `crates/octet-engine/tests/live.rs`, `crates/octet-tui/src/view/tests.rs` (3)
  - `docs/rust/adding-a-provider.md`: the row example
- Test: `crates/octet-engine/tests/live.rs`

**Interfaces:**

- **Produces:**

  ```rust
  // octet_engine::live
  pub struct Config { /* … */ pub catalog_only: bool }   // Config::new sets false
  pub struct Provider { /* … */ pub lists_models: bool }  // Codex true, Claude true, Demo false
  pub const PROBE_LIMIT: Duration;                        // 20 s
  pub async fn probe(engine: Engine, binary: PathBuf, cwd: PathBuf, limit: Duration)
      -> Result<Vec<ModelInfo>, String>;
  ```

- [ ] **Step 1: Write the failing tests** (append to `crates/octet-engine/tests/live.rs`; add `probe, PROBE_LIMIT` to the `use octet_engine::live::{…}` list)

```rust
/// A vendor whose input is logged to `sent.log` beside it.
fn logged(name: &str) -> (octet_testkit::TempDir, std::path::PathBuf) {
    let child = octet_testkit::protocol_child();
    script_vendor(
        name,
        &format!("tee \"$(dirname \"$0\")/sent.log\" | '{}' \"$@\"", child.display()),
    )
}

#[tokio::test]
async fn a_codex_probe_lists_every_page_and_opens_no_thread() {
    let (dir, script) = logged("octet-probe-codex");
    let models = probe(Engine::CODEX, script, std::env::temp_dir(), PROBE_LIMIT)
        .await
        .unwrap();
    let names: Vec<&str> = models.iter().map(|m| m.selection.as_str()).collect();
    assert_eq!(names, ["picker-fixture", "picker-other"]);
    let sent = std::fs::read_to_string(dir.path().join("sent.log")).unwrap();
    assert!(sent.contains("model/list"), "{sent}");
    assert!(!sent.contains("thread/"), "{sent}");
}

#[tokio::test]
async fn a_claude_probe_lists_its_models_and_sends_no_prompt() {
    let (dir, script) = logged("octet-probe-claude");
    let models = probe(Engine::CLAUDE, script, std::env::temp_dir(), PROBE_LIMIT)
        .await
        .unwrap();
    assert_eq!(models[0].selection, "sonnet");
    let sent = std::fs::read_to_string(dir.path().join("sent.log")).unwrap();
    assert!(!sent.contains(r#""type":"user""#), "{sent}");
}

#[tokio::test]
async fn a_silent_cli_gives_up_at_the_limit() {
    let (_dir, script) = script_vendor("octet-probe-silent", "exec sleep 30");
    let started = std::time::Instant::now();
    let error = probe(Engine::CODEX, script, std::env::temp_dir(), Duration::from_millis(300))
        .await
        .unwrap_err();
    assert!(error.contains("did not list its models"), "{error}");
    assert!(started.elapsed() < Duration::from_secs(3));
}

#[tokio::test]
async fn a_provider_without_a_list_is_not_probed() {
    let error = probe(Engine::DEMO, "demo".into(), std::env::temp_dir(), PROBE_LIMIT)
        .await
        .unwrap_err();
    assert!(error.contains("cannot list its models"), "{error}");
}
```

- [ ] **Step 2: Run them to see the expected failure**

  Run: `scripts/rust-env.sh cargo test -p octet-engine --test live probe`

  Expected: a compile failure (`probe`, `PROBE_LIMIT` not found).

- [ ] **Step 3: Add the stubs, then watch the behavioural failure**

  In `mod.rs`:
  - add `pub const PROBE_LIMIT: Duration = Duration::from_secs(20);`
  - add `pub async fn probe(…) -> Result<Vec<ModelInfo>, String> { Err(String::new()) }`, with the signature from Interfaces.

  Run the tests again. Expected: all four FAIL, on `unwrap()` of `Err` and on
  the message assertions.

- [ ] **Step 4: Implement**

  **(a)** `Config` gains its field, and `Config::new` sets it to `false`:

  ```rust
  /// Open the CLI only far enough to list its models: no vendor session,
  /// no prompt. Used by `probe`.
  pub catalog_only: bool,
  ```

  Then add `catalog_only: false,` to every `Config { … }` literal; the build
  error lists them all. In `Selection::configure`
  (`crates/octet-core/src/model.rs`) write `catalog_only: false` too.

  **(b)** `Provider` gains `lists_models`. Set it to `true` in the Codex and
  Claude rows and `false` in the demo row:

  ```rust
  /// The CLI can list its models without opening a session (`probe`).
  pub lists_models: bool,
  ```

  Add the field to the row example in `docs/rust/adding-a-provider.md`, with
  the comment "true if the CLI can list its models without opening a session".

  **(c)** Codex's catalog-only handshake, in `codex.rs` `handshaken`. Right
  after `core.send(json!({"method":"initialized","params":{}})).await?;`:

  ```rust
  if core.config.catalog_only {
      // Only the model list: no thread, so nothing in Codex's history.
      return self.request_models(core, None).await;
  }
  ```

  **(d)** `catalog_page`: in catalog-only mode, a failed page is an error, and
  the last page is followed by `Ready`. Replace the body after
  `self.catalog_pages += 1;` with:

  ```rust
  if let Some(error) = v.get("error") {
      if core.config.catalog_only {
          return Err(format!("Codex did not list its models: {}", error_text(error)).into());
      }
      return core.emit(Event::Notice(
          "Model catalog unavailable from this CLI; explicit model IDs remain supported".into(),
      ));
  }
  for model in catalog(&v["result"]["data"]) {
      if self.catalog.len() < 256 && !self.catalog.iter().any(|m| m.selection == model.selection) {
          self.catalog.push(model);
      }
  }
  let mut more = false;
  if let Some(cursor) = v["result"]["nextCursor"].as_str().filter(|c| !c.is_empty()) {
      if self.catalog_pages < 8
          && self.catalog.len() < 256
          && cursor.len() <= 4096
          && self.catalog_cursors.insert(cursor.to_owned())
      {
          self.request_models(core, Some(cursor)).await?;
          more = true;
      } else {
          core.emit(Event::Notice(
              "Model catalog exceeds discovery limits; showing partial results".into(),
          ))?;
      }
  }
  core.emit(Event::Models(self.catalog.clone()))?;
  if core.config.catalog_only && !more {
      // The whole list is in: a probe takes it now.
      core.emit(Event::Ready { session: String::new() })?;
  }
  Ok(())
  ```

  Claude needs no protocol change. Its initialize reply already emits `Ready`
  and then `Models`, and nothing sends a prompt.

  **(e)** `probe` in `mod.rs`:

  ```rust
  /// The models `engine`'s CLI lists, read without opening a vendor session
  /// or sending a prompt: the CLI starts catalog-only and is stopped once it
  /// has listed them (Codex sends `Ready` after its last page; Claude before
  /// its list).
  ///
  /// # Errors
  ///
  /// The provider cannot list its models, or its CLI could not start,
  /// failed, or did not list them within `limit`.
  pub async fn probe(
      engine: Engine,
      binary: PathBuf,
      cwd: PathBuf,
      limit: Duration,
  ) -> Result<Vec<ModelInfo>, String> {
      let title = engine.title();
      if !engine.provider().lists_models {
          return Err(format!("{title} cannot list its models"));
      }
      let mut config = Config::new(engine, binary, cwd);
      config.catalog_only = true;
      let (handle, mut events, task) = spawn(config);
      let listed = timeout(limit, async {
          let (mut ready, mut models) = (false, None);
          while let Some(event) = events.recv().await {
              match event {
                  Event::Ready { .. } => ready = true,
                  Event::Models(list) => models = Some(list),
                  Event::Error(error) => return Err(error),
                  _ => {}
              }
              if ready && let Some(list) = models.take() {
                  return Ok(list);
              }
          }
          Err(format!("{title} stopped before listing its models"))
      })
      .await
      .unwrap_or_else(|_| {
          Err(format!("{title} did not list its models within {} s", limit.as_secs()))
      });
      handle.shutdown();
      // Let the session stop its CLI; bounded, like any shutdown.
      let _ = timeout(Duration::from_secs(5), async {
          while events.recv().await.is_some() {}
          let _ = task.await;
      })
      .await;
      listed
  }
  ```

  If `title()` is not a method on `Engine`, use `engine.provider().title`.

- [ ] **Step 5: Run the tests and see them pass**

  Run: `scripts/rust-env.sh cargo test -p octet-engine`

  Expected: everything passes, including the 4 new tests.

- [ ] **Step 6: Gate and commit**

  Run: `make rust-check` (expected: exit 0), then:

  ```bash
  git add crates/octet-engine crates/octet-core/src/model.rs crates/octet-core/tests/session.rs \
    crates/octet/src/args.rs crates/octet-tui/src/view/tests.rs docs/rust/adding-a-provider.md
  git commit -m "Engine: a catalog-only mode, and probe() to read a CLI's model list

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

### Task 2: Core — the `catalog` module and its cache

**Files:**

- Create: `crates/octet-core/src/catalog.rs`
- Modify: `crates/octet-core/src/lib.rs` (`pub mod catalog;`)

**Interfaces:**

- **Consumes:** `octet_core::{Engine, ModelInfo}`,
  `octet_engine::live::valid_identifier`.
- **Produces:**

  ```rust
  pub const FILE: &str = "models.json";
  pub const STALE_AFTER: Duration;                       // 24 h
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub struct Listed { pub models: Vec<ModelInfo>, pub fetched: SystemTime }
  #[derive(Debug, Clone, Default, PartialEq, Eq)]
  pub struct Catalogs { /* HashMap<Engine, Listed> */ }
  impl Catalogs {
      pub fn load(path: &Path) -> Self;                   // missing/malformed → empty
      pub fn save(&self, path: &Path) -> io::Result<()>;  // atomic, 0600
      pub fn set(&mut self, engine: Engine, models: Vec<ModelInfo>, at: SystemTime);
      pub fn get(&self, engine: Engine) -> Option<&Listed>;
      pub fn stale(&self, engine: Engine, now: SystemTime) -> bool;
      pub fn owners(&self, name: &str) -> Vec<Engine>;        // lists holding name as selection or ID
      pub fn prefix_owners(&self, name: &str) -> Vec<Engine>; // providers whose listed IDs share name's leading word
  }
  ```

- [ ] **Step 1: Write the failing tests** (in `catalog.rs`, under `#[cfg(test)] mod tests`)

```rust
use super::*;
use std::time::{Duration, UNIX_EPOCH};

fn model(selection: &str, id: Option<&str>) -> ModelInfo {
    ModelInfo {
        selection: selection.into(),
        id: id.map(Into::into),
        name: selection.to_uppercase(),
        description: String::new(),
    }
}
fn sample() -> Catalogs {
    let mut c = Catalogs::default();
    let at = UNIX_EPOCH + Duration::from_secs(1_000_000);
    c.set(Engine::CLAUDE, vec![model("default", Some("claude-fable-5-1")), model("opus", Some("claude-opus-5-5"))], at);
    c.set(Engine::CODEX, vec![model("gpt-6-astra", Some("gpt-6-astra")), model("gpt-5.6-sol", Some("gpt-5.6-sol"))], at);
    c
}

#[test]
fn owners_are_found_by_selection_or_id() {
    let c = sample();
    assert_eq!(c.owners("opus"), [Engine::CLAUDE]);
    assert_eq!(c.owners("claude-opus-5-5"), [Engine::CLAUDE]);
    assert_eq!(c.owners("gpt-6-astra"), [Engine::CODEX]);
    assert!(c.owners("gpt-5.5").is_empty());
}

#[test]
fn prefixes_are_learned_from_listed_ids() {
    let c = sample();
    assert_eq!(c.prefix_owners("gpt-5.5"), [Engine::CODEX]);
    assert_eq!(c.prefix_owners("claude-opus-9"), [Engine::CLAUDE]);
    assert!(c.prefix_owners("mistral-large").is_empty());
    assert!(c.prefix_owners("opus").is_empty(), "a name without a leading word");
}

#[test]
fn a_list_is_stale_when_missing_or_a_day_old() {
    let c = sample();
    let fetched = c.get(Engine::CODEX).unwrap().fetched;
    assert!(!c.stale(Engine::CODEX, fetched + Duration::from_secs(60)));
    assert!(c.stale(Engine::CODEX, fetched + STALE_AFTER));
    assert!(Catalogs::default().stale(Engine::CODEX, fetched));
}

#[test]
fn the_cache_round_trips_privately() {
    let dir = octet_testkit::TempDir::new("octet-catalog");
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.path().join(FILE);
    sample().save(&path).unwrap();
    assert_eq!(Catalogs::load(&path), sample());
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
}

#[test]
fn a_bad_cache_is_skipped_entry_by_entry() {
    let dir = octet_testkit::TempDir::new("octet-catalog-bad");
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.path().join(FILE);
    std::fs::write(&path, "not json").unwrap();
    assert_eq!(Catalogs::load(&path), Catalogs::default());
    std::fs::write(
        &path,
        r#"{"codex":{"fetched":5,"models":[{"selection":"gpt-ok","name":"ok"},{"selection":"bad\u0007"},{"selection":7}]},
            "demo":{"fetched":5,"models":[{"selection":"x"}]},
            "nobody":{"fetched":5,"models":[]},
            "claude":{"fetched":"soon","models":[]}}"#,
    )
    .unwrap();
    let loaded = Catalogs::load(&path);
    let codex = loaded.get(Engine::CODEX).unwrap();
    assert_eq!(codex.models.len(), 1);
    assert_eq!(codex.models[0].selection, "gpt-ok");
    assert!(loaded.get(Engine::CLAUDE).is_none());
    assert!(loaded.get(Engine::DEMO).is_none());
}

#[test]
fn parallel_saves_leave_a_whole_file() {
    let dir = octet_testkit::TempDir::new("octet-catalog-race");
    std::fs::create_dir_all(dir.path()).unwrap();
    let path = dir.path().join(FILE);
    std::thread::scope(|scope| {
        for _ in 0..8 {
            scope.spawn(|| {
                for _ in 0..20 {
                    sample().save(&path).unwrap();
                }
            });
        }
    });
    assert_eq!(Catalogs::load(&path), sample());
}
```

  If `octet-testkit` is not already a dev-dependency of `octet-core`, add
  `octet-testkit.workspace = true` under `[dev-dependencies]` in
  `crates/octet-core/Cargo.toml`.

- [ ] **Step 2: Run them to see the expected failure**

  Run: `scripts/rust-env.sh cargo test -p octet-core --lib catalog`

  Expected: a compile failure (no module `catalog`, `Catalogs` unknown).

- [ ] **Step 3: Implement `catalog.rs`**

```rust
//! Each provider's model list as its CLI last reported it, cached on disk so
//! `/model` can offer every provider's models. Nothing here names a vendor:
//! the lists come from the providers, and their prefixes are learned from them.
use crate::{Engine, ModelInfo};
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    io::{self, Write as _},
    os::unix::fs::OpenOptionsExt as _,
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

/// The cache file's name, in Octet's data directory.
pub const FILE: &str = "models.json";
/// A list this old is fetched again when next needed.
pub const STALE_AFTER: Duration = Duration::from_secs(24 * 60 * 60);
/// The most models kept per provider, as the drivers cap a live list.
const MAX_MODELS: usize = 256;

/// One provider's list and when it was fetched.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Listed {
    /// The models, in the provider's order.
    pub models: Vec<ModelInfo>,
    /// When the provider reported them.
    pub fetched: SystemTime,
}

/// Every provider's last list.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Catalogs {
    lists: HashMap<Engine, Listed>,
}

impl Catalogs {
    /// The lists cached at `path`. A missing or malformed file, or a bad
    /// entry in it, is skipped: the cache only ever saves a fetch.
    pub fn load(path: &Path) -> Self {
        let Some(root) = std::fs::read(path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        else {
            return Self::default();
        };
        let mut lists = HashMap::new();
        for (name, entry) in root.as_object().into_iter().flatten() {
            let Some(engine) = Engine::parse(name).filter(|engine| engine.is_vendor()) else {
                continue;
            };
            let Some(seconds) = entry["fetched"].as_u64() else {
                continue;
            };
            let models = entry["models"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(model_from_json)
                .take(MAX_MODELS)
                .collect();
            lists.insert(
                engine,
                Listed {
                    models,
                    fetched: UNIX_EPOCH + Duration::from_secs(seconds),
                },
            );
        }
        Self { lists }
    }

    /// Writes the lists to `path`: a private temporary file renamed over it,
    /// so a reader or another window never sees half a file.
    ///
    /// # Errors
    ///
    /// The file could not be written or renamed.
    pub fn save(&self, path: &Path) -> io::Result<()> {
        let mut root = Map::new();
        for (engine, listed) in &self.lists {
            let seconds = listed
                .fetched
                .duration_since(UNIX_EPOCH)
                .map_or(0, |since| since.as_secs());
            let models: Vec<Value> = listed.models.iter().map(model_to_json).collect();
            root.insert(engine.as_str().into(), json!({"fetched": seconds, "models": models}));
        }
        let bytes = serde_json::to_vec(&Value::Object(root)).map_err(io::Error::other)?;
        // One temporary name per process and thread: parallel savers never share it.
        let temporary = path.with_extension(format!(
            "json.{}.{:?}.tmp",
            std::process::id(),
            std::thread::current().id()
        ));
        let written = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&temporary)
            .and_then(|mut file| {
                file.write_all(&bytes)?;
                file.sync_all()
            })
            .and_then(|()| std::fs::rename(&temporary, path));
        if written.is_err() {
            let _ = std::fs::remove_file(&temporary);
        }
        written
    }

    /// Replaces `engine`'s list.
    pub fn set(&mut self, engine: Engine, mut models: Vec<ModelInfo>, at: SystemTime) {
        models.truncate(MAX_MODELS);
        self.lists.insert(engine, Listed { models, fetched: at });
    }

    /// `engine`'s list, if one was ever fetched.
    pub fn get(&self, engine: Engine) -> Option<&Listed> {
        self.lists.get(&engine)
    }

    /// Whether `engine`'s list is missing or older than `STALE_AFTER` (a
    /// time in the future, from a changed clock, counts as stale).
    pub fn stale(&self, engine: Engine, now: SystemTime) -> bool {
        self.get(engine).is_none_or(|listed| {
            now.duration_since(listed.fetched)
                .map_or(true, |age| age >= STALE_AFTER)
        })
    }

    /// The providers whose list has `name` as a selection or full ID.
    pub fn owners(&self, name: &str) -> Vec<Engine> {
        vendors()
            .filter(|engine| {
                self.get(*engine).is_some_and(|listed| {
                    listed
                        .models
                        .iter()
                        .any(|m| m.selection == name || m.id.as_deref() == Some(name))
                })
            })
            .collect()
    }

    /// The providers whose listed IDs share `name`'s leading word (up to its
    /// first `-`): `gpt` for `gpt-5.5` when a provider lists `gpt-6-astra`.
    pub fn prefix_owners(&self, name: &str) -> Vec<Engine> {
        let Some((word, _)) = name.split_once('-') else {
            return Vec::new();
        };
        vendors()
            .filter(|engine| {
                self.get(*engine).is_some_and(|listed| {
                    listed.models.iter().any(|m| {
                        m.id.as_deref()
                            .unwrap_or(&m.selection)
                            .split_once('-')
                            .is_some_and(|(lead, _)| !lead.is_empty() && lead == word)
                    })
                })
            })
            .collect()
    }
}

/// Every vendor provider, in table order.
fn vendors() -> impl Iterator<Item = Engine> {
    Engine::ALL.iter().copied().filter(|engine| engine.is_vendor())
}

/// A cached model, if it is a usable one.
fn model_from_json(value: &Value) -> Option<ModelInfo> {
    let valid = |s: &str| octet_engine::live::valid_identifier(s);
    let selection = value["selection"].as_str().filter(|s| valid(s))?;
    let text = |key: &str, limit: usize| {
        value[key]
            .as_str()
            .filter(|s| s.len() <= limit && !s.chars().any(char::is_control))
            .unwrap_or_default()
            .to_owned()
    };
    Some(ModelInfo {
        selection: selection.to_owned(),
        id: value["id"].as_str().filter(|s| valid(s)).map(str::to_owned),
        name: text("name", 256),
        description: text("description", 1024),
    })
}

fn model_to_json(model: &ModelInfo) -> Value {
    json!({
        "selection": model.selection,
        "id": model.id,
        "name": model.name,
        "description": model.description,
    })
}
```

  Add `pub mod catalog;` to `crates/octet-core/src/lib.rs`, next to
  `pub mod model;`. If `Engine` or `ModelInfo` are not re-exported at the
  crate root, import them from `octet_engine::live`.

- [ ] **Step 4: Run the tests and see them pass**

  Run: `scripts/rust-env.sh cargo test -p octet-core --lib catalog`

  Expected: 6 passed.

- [ ] **Step 5: Gate and commit**

  Run: `make rust-check` (expected: exit 0), then:

  ```bash
  git add crates/octet-core/src/catalog.rs crates/octet-core/src/lib.rs crates/octet-core/Cargo.toml
  git commit -m "Core: every provider's model list, cached in models.json

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

### Task 3: Core — `Selection::resolve`

**Files:**

- Modify: `crates/octet-core/src/model.rs`; `crates/octet-core/src/error.rs`
  (`SelectionError::Ambiguous`)

**Interfaces:**

- **Consumes:** `Catalogs::{owners, prefix_owners}` (Task 2).
- **Produces:**

  ```rust
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub enum Found { Named, Listed, Prefix(String), Current }
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub struct Resolved { pub selection: Selection, pub found: Found }
  impl Selection {
      pub fn resolve(input: &str, current: Engine, catalogs: &Catalogs)
          -> Result<Resolved, SelectionError>;
  }
  // SelectionError::Ambiguous { name: String, providers: String }
  ```

- [ ] **Step 1: Write the failing tests** (in `model.rs` tests)

```rust
fn lists() -> crate::catalog::Catalogs {
    let mut c = crate::catalog::Catalogs::default();
    let model = |s: &str| crate::ModelInfo {
        selection: s.into(),
        id: Some(s.into()),
        name: s.into(),
        description: String::new(),
    };
    let now = std::time::SystemTime::now();
    c.set(Engine::CLAUDE, vec![model("default"), model("opus"), model("claude-opus-5-5"), model("shared")], now);
    c.set(Engine::CODEX, vec![model("gpt-6-astra"), model("gpt-5.6-sol"), model("shared")], now);
    c
}
fn resolved(input: &str, current: Engine) -> (Engine, Option<String>, Found) {
    let r = Selection::resolve(input, current, &lists()).unwrap();
    (r.selection.provider, r.selection.model, r.found)
}

#[test]
fn a_listed_name_picks_its_provider() {
    assert_eq!(resolved("opus", Engine::CODEX), (Engine::CLAUDE, Some("opus".into()), Found::Listed));
    assert_eq!(resolved("gpt-6-astra", Engine::CLAUDE), (Engine::CODEX, Some("gpt-6-astra".into()), Found::Listed));
}

#[test]
fn default_and_unknown_names_stay_with_the_current_provider() {
    assert_eq!(resolved("default", Engine::CODEX), (Engine::CODEX, None, Found::Current));
    assert_eq!(resolved("my-custom", Engine::CLAUDE), (Engine::CLAUDE, Some("my-custom".into()), Found::Current));
}

#[test]
fn an_unlisted_name_goes_by_its_learned_prefix() {
    assert_eq!(
        resolved("gpt-5.5", Engine::CLAUDE),
        (Engine::CODEX, Some("gpt-5.5".into()), Found::Prefix("gpt".into()))
    );
}

#[test]
fn a_name_in_several_lists_stays_current_or_is_refused() {
    assert_eq!(resolved("shared", Engine::CODEX).0, Engine::CODEX);
    assert_eq!(resolved("shared", Engine::CLAUDE).0, Engine::CLAUDE);
    let mut only_others = lists();
    only_others.set(Engine::CLAUDE, Vec::new(), std::time::SystemTime::now());
    only_others.set(Engine::CODEX, Vec::new(), std::time::SystemTime::now());
    // With no list holding it, it is the current provider's custom name.
    assert_eq!(Selection::resolve("shared", Engine::CODEX, &only_others).unwrap().found, Found::Current);
}

#[test]
fn explicit_forms_are_unchanged() {
    for (input, provider, model) in [
        ("codex opus", Engine::CODEX, Some("opus")),
        ("claude/gpt-6-astra", Engine::CLAUDE, Some("gpt-6-astra")),
        ("claude", Engine::CLAUDE, None),
    ] {
        let r = Selection::resolve(input, Engine::CODEX, &lists()).unwrap();
        assert_eq!((r.selection.provider, r.selection.model.as_deref(), r.found), (provider, model, Found::Named));
    }
}
```

  The refusal arm needs a name in two lists, neither of them the current
  provider's. With only two vendors that can't happen, so it is tested
  directly in Step 4.

- [ ] **Step 2: Run them to see the expected failure**

  Run: `scripts/rust-env.sh cargo test -p octet-core --lib model`

  Expected: a compile failure (`resolve`, `Found` not found).

- [ ] **Step 3: Implement**

  In `error.rs`, add to `SelectionError`:

  ```rust
  /// The name is in several providers' lists, none of them the current one.
  #[error("{name} is in the {providers} model lists; choose one: /model <provider> {name}")]
  Ambiguous {
      /// The model name.
      name: String,
      /// The providers listing it, "claude and codex".
      providers: String,
  },
  ```

  In `model.rs`:

  ```rust
  use crate::catalog::Catalogs;

  /// Where `/model`'s name was found.
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub enum Found {
      /// The provider was named: `/model codex X`, `codex/X`, `/model claude`.
      Named,
      /// In the chosen provider's list.
      Listed,
      /// By a leading word its listed IDs share (`gpt` for `gpt-5.5`).
      Prefix(String),
      /// Nowhere else: the current provider (and always for `default`).
      Current,
  }

  /// A `/model` argument resolved against every provider's list.
  #[derive(Debug, Clone, PartialEq, Eq)]
  pub struct Resolved {
      /// The provider and model.
      pub selection: Selection,
      /// Where the name was found.
      pub found: Found,
  }

  impl Selection {
      /// Resolves a `/model` argument. A bare name goes to the provider whose
      /// list holds it, else to the one whose listed IDs share its leading
      /// word, else to `current`; `default` and the explicit forms
      /// (`PROVIDER [NAME]`, `PROVIDER/NAME`) resolve as `parse` does.
      ///
      /// # Errors
      ///
      /// As `parse`; and a bare name in several providers' lists, none of
      /// them `current`, is ambiguous.
      pub fn resolve(input: &str, current: Engine, catalogs: &Catalogs) -> Result<Resolved, SelectionError> {
          let named = |word: &str| Engine::parse(word).is_some_and(|engine| engine.is_vendor());
          let words: Vec<&str> = input.split_whitespace().collect();
          let bare = match words.as_slice() {
              [word]
                  if !named(word)
                      && word.split_once('/').is_none_or(|(prefix, _)| !named(prefix)) =>
              {
                  *word
              }
              _ => {
                  return Self::parse(input, current).map(|selection| Resolved {
                      selection,
                      found: Found::Named,
                  });
              }
          };
          let owners = catalogs.owners(bare);
          let (provider, found) = if bare == "default" {
              (current, Found::Current)
          } else if owners.contains(&current) {
              (current, Found::Listed)
          } else if let [owner] = owners.as_slice() {
              (*owner, Found::Listed)
          } else if !owners.is_empty() {
              return Err(ambiguous(bare, &owners));
          } else if let [owner] = catalogs.prefix_owners(bare).as_slice() {
              let word = bare.split_once('-').map_or("", |(word, _)| word);
              (*owner, Found::Prefix(word.to_owned()))
          } else {
              (current, Found::Current)
          };
          let selection = Self::parse(&format!("{provider} {bare}"), current)?;
          Ok(Resolved { selection, found })
      }
  }

  fn ambiguous(name: &str, owners: &[Engine]) -> SelectionError {
      SelectionError::Ambiguous {
          name: name.to_owned(),
          providers: owners
              .iter()
              .map(Engine::as_str)
              .collect::<Vec<_>>()
              .join(" and "),
      }
  }
  ```

  Note the order: "in the current provider's list" is checked before "in
  exactly one list", so a name both providers list stays current. That
  matches the spec ("stays with the current provider if that is one of
  them").

- [ ] **Step 4: Add the refusal test**

```rust
#[test]
fn several_other_lists_are_refused_with_a_hint() {
    let error = ambiguous("shared", &[Engine::CLAUDE, Engine::CODEX]);
    assert_eq!(
        error.to_string(),
        "shared is in the claude and codex model lists; choose one: /model <provider> shared"
    );
}
```

- [ ] **Step 5: Run the tests and see them pass**

  Run: `scripts/rust-env.sh cargo test -p octet-core`

  Expected: all pass, including the new 6.

- [ ] **Step 6: Gate and commit**

  Run: `make rust-check` (expected: exit 0), then:

  ```bash
  git add crates/octet-core/src/model.rs crates/octet-core/src/error.rs
  git commit -m "Core: /model NAME resolves against every provider's list

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

### Task 4: TUI — the `models` state, probes, `/model NAME` and `/model refresh`

**Files:**

- Create: `crates/octet-tui/src/models.rs`
- Modify:
  - `crates/octet-tui/src/lib.rs`: `mod models;`; `run()` attaches the state and
    carries it into a fresh interface; `session_event` probes after a ready
  - `crates/octet-tui/src/app.rs`: the `App.models` field; `Event::Models`
    feeds the cache
  - `crates/octet-tui/src/event_loop.rs`: the `Wake::Probed` arm
  - `crates/octet-tui/src/commands.rs`: `/model refresh`; `/model NAME`
    through `resolve`
  - `crates/octet-tui/src/registry.rs`: the `/model` usage line
- Test: `crates/octet-tui/src/models.rs`, `crates/octet-tui/src/tests/slash.rs`

**Interfaces:**

- **Consumes:** `octet_engine::live::{probe, PROBE_LIMIT}` (Task 1); `Catalogs`
  (Task 2); `Selection::resolve`, `Found`, `Resolved` (Task 3).
- **Produces:**

  ```rust
  pub(crate) struct Probed { pub(crate) engine: Engine, pub(crate) result: Result<Vec<ModelInfo>, String> }
  pub(crate) struct Prober { pub(crate) cwd: PathBuf, pub(crate) binary: fn(Engine) -> PathBuf, pub(crate) limit: Duration }
  pub(crate) struct Models { pub(crate) catalogs: Catalogs, /* … */ }   // Default: no cache file, no probes
  impl Models {
      pub(crate) fn attach(&mut self, directory: &Path, prober: Prober);
      pub(crate) fn is_attached(&self) -> bool;
      pub(crate) fn probe_stale(&mut self, active: Engine, now: SystemTime);
      pub(crate) fn refresh(&mut self, active: Engine) -> Vec<Engine>;
      pub(crate) async fn next(&mut self) -> Probed;               // cancel-safe
      pub(crate) fn probed(&mut self, probed: Probed, now: SystemTime) -> Option<String>;
      pub(crate) fn live(&mut self, engine: Engine, models: Vec<ModelInfo>, now: SystemTime) -> Option<String>;
      pub(crate) fn freshness(&self, engine: Engine, live: bool, now: SystemTime) -> String;
      pub(crate) fn names(&self, active: Engine) -> Vec<(String, Engine)>;
  }
  ```

- [ ] **Step 1: Write the failing tests** (in `models.rs` `#[cfg(test)] mod tests`)

```rust
use super::*;
use octet_core::Engine;
use std::time::{Duration, SystemTime};

fn model(s: &str) -> ModelInfo {
    ModelInfo { selection: s.into(), id: Some(s.into()), name: s.into(), description: String::new() }
}
fn attached(dir: &Path) -> Models {
    let mut models = Models::default();
    models.attach(
        dir,
        Prober {
            cwd: std::env::temp_dir(),
            binary: |_| octet_testkit::protocol_child(),
            limit: octet_engine::live::PROBE_LIMIT,
        },
    );
    models
}

#[tokio::test]
async fn a_stale_provider_is_probed_once_and_cached() {
    let dir = octet_testkit::TempDir::new("octet-models-probe");
    std::fs::create_dir_all(dir.path()).unwrap();
    let mut models = attached(dir.path());
    models.probe_stale(Engine::CLAUDE, SystemTime::now());
    // A second request while one runs starts nothing.
    assert!(models.refresh(Engine::CLAUDE).is_empty());
    let probed = models.next().await;
    assert_eq!(probed.engine, Engine::CODEX);
    assert!(models.probed(probed, SystemTime::now()).is_none());
    let cached = Catalogs::load(&dir.path().join(octet_core::catalog::FILE));
    assert!(cached.get(Engine::CODEX).is_some_and(|l| !l.models.is_empty()));
    // Fresh now: no probe.
    models.probe_stale(Engine::CLAUDE, SystemTime::now());
    assert!(models.probing.is_empty());
}

#[test]
fn a_failed_provider_is_not_probed_again_until_refresh() {
    let mut models = Models::default();
    models.failed.insert(Engine::CODEX, "not signed in".into());
    assert!(models.freshness(Engine::CODEX, false, SystemTime::now()).starts_with("unavailable: not signed in"));
    models.probe_stale(Engine::CLAUDE, SystemTime::now());
    assert!(models.probing.is_empty());
}

#[test]
fn freshness_says_where_a_list_came_from() {
    let mut models = Models::default();
    let now = SystemTime::now();
    assert_eq!(models.freshness(Engine::CODEX, false, now), "not fetched");
    models.catalogs.set(Engine::CODEX, vec![model("a-1")], now - Duration::from_secs(2 * 3600));
    assert_eq!(models.freshness(Engine::CODEX, false, now), "cached 2 h ago");
    assert_eq!(models.freshness(Engine::CODEX, true, now), "live");
}

#[test]
fn names_put_the_active_provider_first_once_each() {
    let mut models = Models::default();
    let now = SystemTime::now();
    models.catalogs.set(Engine::CODEX, vec![model("gpt-6-astra")], now);
    models.catalogs.set(Engine::CLAUDE, vec![model("opus"), model("opus")], now);
    let names = models.names(Engine::CLAUDE);
    assert_eq!(names, [("opus".into(), Engine::CLAUDE), ("gpt-6-astra".into(), Engine::CODEX)]);
}
```

  Then, in `crates/octet-tui/src/tests/slash.rs`, follow the existing slash
  tests' helpers (`app_for`, sending a command line, matching `Action`):

```rust
#[test]
fn a_bare_model_name_switches_to_the_provider_that_lists_it() {
    let mut app = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    app.event(octet_core::Event::Ready { session: String::new() });
    app.models.catalogs.set(
        octet_core::Engine::CODEX,
        vec![octet_core::ModelInfo {
            selection: "gpt-6-astra".into(),
            id: Some("gpt-6-astra".into()),
            name: "GPT-6-Astra".into(),
            description: String::new(),
        }],
        std::time::SystemTime::now(),
    );
    let action = slash(&mut app, "/model gpt-6-astra");
    assert!(
        matches!(&action, Action::Exit(Exit::Model(s)) if s.provider == octet_core::Engine::CODEX),
        "{action:?}"
    );
    assert!(app.entries_text().contains("gpt-6-astra is in Codex's model list"));
}

#[test]
fn a_live_list_is_cached() {
    let dir = octet_testkit::TempDir::new("octet-live-list");
    std::fs::create_dir_all(dir.path()).unwrap();
    let mut app = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    app.models.attach(
        dir.path(),
        crate::models::Prober {
            cwd: std::env::temp_dir(),
            binary: |_| octet_testkit::protocol_child(),
            limit: octet_engine::live::PROBE_LIMIT,
        },
    );
    app.event(octet_core::Event::Models(vec![octet_core::ModelInfo {
        selection: "opus".into(),
        id: None,
        name: "Opus".into(),
        description: String::new(),
    }]));
    let cached = octet_core::catalog::Catalogs::load(&dir.path().join(octet_core::catalog::FILE));
    assert_eq!(cached.get(octet_core::Engine::CLAUDE).unwrap().models[0].selection, "opus");
}

#[test]
fn model_refresh_without_a_data_directory_says_so() {
    let mut app = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    let _ = slash(&mut app, "/model refresh");
    assert!(app.entries_text().contains("No other provider's model list to fetch"));
}
```

  `slash` stands for the file's existing helper that runs one command line
  and returns the `Action`; use its real name. If `Action` lacks `Debug`, use
  `matches!` without the message.

- [ ] **Step 2: Run them to see the expected failure**

  Run: `scripts/rust-env.sh cargo test -p octet-tui --lib models slash`

  Expected: a compile failure (no `models` module or field).

- [ ] **Step 3: Implement `models.rs`**

```rust
//! Every provider's model list for `/model`: the cache, the probes that
//! refresh it, and how fresh each list is. Nothing here names a vendor.
use octet_core::{Engine, ModelInfo, catalog::{self, Catalogs}};
use std::{
    collections::{HashMap, HashSet},
    path::{Path, PathBuf},
    time::{Duration, SystemTime},
};
use tokio::sync::mpsc;

/// A probe's result.
#[derive(Debug)]
pub(crate) struct Probed {
    pub(crate) engine: Engine,
    pub(crate) result: Result<Vec<ModelInfo>, String>,
}

/// Where probes run and which CLI each provider's starts.
#[derive(Debug, Clone)]
pub(crate) struct Prober {
    pub(crate) cwd: PathBuf,
    pub(crate) binary: fn(Engine) -> PathBuf,
    pub(crate) limit: Duration,
}

/// The lists, their cache file and the probes in flight.
#[derive(Debug)]
pub(crate) struct Models {
    pub(crate) catalogs: Catalogs,
    /// The cache file; `None` keeps the lists in memory only.
    path: Option<PathBuf>,
    /// Set once the interface may start probes.
    prober: Option<Prober>,
    pub(crate) probing: HashSet<Engine>,
    /// Probes that failed this run: not retried until `/model refresh`.
    pub(crate) failed: HashMap<Engine, String>,
    tx: mpsc::UnboundedSender<Probed>,
    rx: mpsc::UnboundedReceiver<Probed>,
}

impl Default for Models {
    fn default() -> Self {
        let (tx, rx) = mpsc::unbounded_channel();
        Self {
            catalogs: Catalogs::default(),
            path: None,
            prober: None,
            probing: HashSet::new(),
            failed: HashMap::new(),
            tx,
            rx,
        }
    }
}

impl Models {
    /// Loads the cache from `directory` and allows probes.
    pub(crate) fn attach(&mut self, directory: &Path, prober: Prober) {
        let path = directory.join(catalog::FILE);
        self.catalogs = Catalogs::load(&path);
        self.path = Some(path);
        self.prober = Some(prober);
    }

    pub(crate) fn is_attached(&self) -> bool {
        self.path.is_some()
    }

    /// Probes every other provider whose list is missing or stale, unless a
    /// probe of it failed this run.
    pub(crate) fn probe_stale(&mut self, active: Engine, now: SystemTime) {
        for engine in others(active) {
            if self.catalogs.stale(engine, now) && !self.failed.contains_key(&engine) {
                self.probe(engine);
            }
        }
    }

    /// `/model refresh`: probes every other provider now. The providers it
    /// started probing.
    pub(crate) fn refresh(&mut self, active: Engine) -> Vec<Engine> {
        others(active).filter(|engine| {
            self.failed.remove(engine);
            self.probe(*engine)
        }).collect()
    }

    /// Starts a probe of `engine`; false when one runs, the provider cannot
    /// list its models, or probes are not allowed.
    fn probe(&mut self, engine: Engine) -> bool {
        let Some(prober) = self.prober.clone() else {
            return false;
        };
        if !engine.provider().lists_models || !self.probing.insert(engine) {
            return false;
        }
        let tx = self.tx.clone();
        tokio::spawn(async move {
            let result = octet_engine::live::probe(engine, (prober.binary)(engine), prober.cwd, prober.limit).await;
            let _ = tx.send(Probed { engine, result });
        });
        true
    }

    /// The next probe result. Waits forever while none runs; cancel-safe.
    pub(crate) async fn next(&mut self) -> Probed {
        match self.rx.recv().await {
            Some(probed) => probed,
            // Unreachable while `tx` lives, which it does as long as `self`.
            None => std::future::pending().await,
        }
    }

    /// A probe ended. A note when the lists could not be saved.
    pub(crate) fn probed(&mut self, probed: Probed, now: SystemTime) -> Option<String> {
        self.probing.remove(&probed.engine);
        match probed.result {
            Ok(models) => {
                self.failed.remove(&probed.engine);
                self.catalogs.set(probed.engine, models, now);
                self.save()
            }
            Err(error) => {
                self.failed.insert(probed.engine, error);
                None
            }
        }
    }

    /// The active provider's live list. A note when it could not be saved.
    pub(crate) fn live(&mut self, engine: Engine, models: Vec<ModelInfo>, now: SystemTime) -> Option<String> {
        if models.is_empty() {
            return None;
        }
        self.catalogs.set(engine, models, now);
        self.save()
    }

    fn save(&self) -> Option<String> {
        let path = self.path.as_ref()?;
        self.catalogs
            .save(path)
            .err()
            .map(|error| format!("The model lists could not be cached: {error}"))
    }

    /// How fresh `engine`'s list is, for the `/model` header. `live`: the
    /// connected session reported it.
    pub(crate) fn freshness(&self, engine: Engine, live: bool, now: SystemTime) -> String {
        if live {
            return "live".into();
        }
        if self.probing.contains(&engine) {
            return "probing…".into();
        }
        match (self.catalogs.get(engine), self.failed.get(&engine)) {
            (None, Some(error)) => format!("unavailable: {}", error.lines().next().unwrap_or_default()),
            (None, None) => "not fetched".into(),
            (Some(listed), _) => format!("cached {}", ago(now, listed.fetched)),
        }
    }

    /// Model names for Tab: selections and full IDs once each, with their
    /// provider, the active provider's first.
    pub(crate) fn names(&self, active: Engine) -> Vec<(String, Engine)> {
        let mut seen = HashSet::new();
        std::iter::once(active)
            .chain(others(active))
            .flat_map(|engine| {
                self.catalogs.get(engine).into_iter().flat_map(move |listed| {
                    listed.models.iter().flat_map(move |m| {
                        std::iter::once(m.selection.clone())
                            .chain(m.id.clone())
                            .map(move |name| (name, engine))
                    })
                })
            })
            .filter(|(name, _)| seen.insert(name.clone()))
            .collect()
    }
}

/// Every vendor provider but `active`, in table order.
fn others(active: Engine) -> impl Iterator<Item = Engine> {
    Engine::ALL.iter().copied().filter(move |engine| engine.is_vendor() && *engine != active)
}

/// "just now", "5 min ago", "2 h ago", "3 days ago".
fn ago(now: SystemTime, then: SystemTime) -> String {
    let seconds = now.duration_since(then).map_or(0, |age| age.as_secs());
    match seconds {
        0..60 => "just now".into(),
        60..3600 => format!("{} min ago", seconds / 60),
        3600..86_400 => format!("{} h ago", seconds / 3600),
        _ => format!("{} days ago", seconds / 86_400),
    }
}
```

  Register the module with `mod models;` in `lib.rs`.

- [ ] **Step 4: Wire it in**

  1. **`app.rs`:** add `pub(crate) models: crate::models::Models,` to `App`,
     initialised `Default::default()` in `App::new`. In `App::event`, change
     the `Event::Models(models)` arm to:

     ```rust
     Event::Models(models) => {
         if let Some(note) = self.models.live(self.conn.engine, models.clone(), std::time::SystemTime::now()) {
             self.note(note);
         }
         self.conn.models = models;
         self.refresh_model_label();
     }
     ```

  2. **`lib.rs` `run()`:**
     - Next to the goal-store attach, attach the model state:

       ```rust
       if !app.models.is_attached() {
           app.models.attach(
               &directory,
               models::Prober {
                   cwd: config.cwd.clone(),
                   binary: |engine| std::path::PathBuf::from(engine.provider().default_binary),
                   limit: octet_engine::live::PROBE_LIMIT,
               },
           );
       }
       ```

     - Carry the state into a fresh interface, the way `carried_job` is
       carried. Declare `let mut carried_models: Option<models::Models> = None;`
       beside `carried_job`. Where `!plan.keep_app` takes the job, also take
       `carried_models = Some(std::mem::take(&mut app.models));`. After a new
       `App` is made, write `if let Some(models) = carried_models.take() { app.models = models; }`.

  3. **`lib.rs` `session_event`:** before `app.event(event)`, add
     `let ready = matches!(&event, octet_core::Event::Ready { .. });`. After it:

     ```rust
     if ready && app.is_idle() {
         app.models.probe_stale(app.conn.engine, std::time::SystemTime::now());
     }
     ```

  4. **`event_loop.rs`:**
     - Add the variant `/// A model-list probe ended. Probed(crate::models::Probed),` to `Wake`.
     - In `wake`, add the disjoint borrow `let models = &mut app.models;` and
       the arm `probed = models.next() => Wake::Probed(probed),`.
     - In `handle`:

       ```rust
       Wake::Probed(probed) => {
           if let Some(note) = app.models.probed(probed, std::time::SystemTime::now()) {
               app.note(note);
           }
       }
       ```

     - Mark the frame dirty, as the other arms do.

  5. **`commands.rs` `model_command`:**
     - Add an arm before the `can_reconnect` check:

       ```rust
       } else if argument == "refresh" {
           let started = app.models.refresh(app.conn.engine);
           if started.is_empty() {
               app.note("No other provider's model list to fetch");
           } else {
               let names: Vec<&str> = started.iter().map(|e| e.as_str()).collect();
               app.note(format!("Fetching the model lists of {}…", names.join(", ")));
           }
       ```

     - Replace the `Selection::parse` match with:

       ```rust
       match octet_core::model::Selection::resolve(argument, app.conn.engine, &app.models.catalogs) {
           Ok(resolved) => {
               if let Some(note) = found_note(&resolved, app.conn.engine) {
                   app.note(note);
               }
               return Action::Exit(Exit::Model(resolved.selection));
           }
           Err(error) => app.note(error.to_string()),
       }
       ```

     - Add the helper:

       ```rust
       /// Says where a name was found when it moves to another provider.
       fn found_note(resolved: &octet_core::model::Resolved, current: octet_core::Engine) -> Option<String> {
           use octet_core::model::Found;
           let selection = &resolved.selection;
           if selection.provider == current {
               return None;
           }
           let model = selection.model.as_deref()?;
           let title = selection.provider.title();
           match &resolved.found {
               Found::Listed => Some(format!("{model} is in {title}'s model list")),
               Found::Prefix(word) => Some(format!("{model} goes to {title}: its listed models start with {word}-")),
               Found::Named | Found::Current => None,
           }
       }
       ```

  6. **`registry.rs`:** change the `/model` usage line to
     `"/model [provider] <name> · /model default · /model refresh"`.

- [ ] **Step 5: Run the tests and see them pass**

  Run: `scripts/rust-env.sh cargo test -p octet-tui --lib`

  Expected: all pass, including the 7 new ones. The probe test uses the fake
  CLI through `octet_testkit::protocol_child()`.

- [ ] **Step 6: Gate and commit**

  Run: `make rust-check` (expected: exit 0), then:

  ```bash
  git add crates/octet-tui/src/models.rs crates/octet-tui/src/lib.rs crates/octet-tui/src/app.rs \
    crates/octet-tui/src/event_loop.rs crates/octet-tui/src/commands.rs crates/octet-tui/src/registry.rs \
    crates/octet-tui/src/tests/slash.rs
  git commit -m "TUI: fetch other providers' model lists when needed; /model NAME picks the provider

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

### Task 5: TUI — one merged list in `/model`, and Tab completion

**Files:**

- Modify:
  - `crates/octet-tui/src/app.rs`: `show_models`, `catalog_hint`, a new
    `model_entries`
  - `crates/octet-tui/src/composer.rs`: `Kind::Model`, `model_tab`
  - `crates/octet-tui/src/input.rs`: the Tab handler, `accept_completion`
  - `crates/octet-tui/src/view.rs`: the popup title for `Kind::Model`
- Test: `crates/octet-tui/src/view/tests.rs`,
  `crates/octet-tui/src/composer.rs`, `crates/octet-tui/src/tests/keys.rs`

**Interfaces:**

- **Consumes:** `Models::{catalogs, freshness, names}` (Task 4);
  `Selection::resolve` (Task 3).
- **Produces:**
  - `composer::Kind::Model`;
  - `composer::model_tab(text: &str, cursor: usize, names: &[(String, Engine)]) -> Option<Tab>`.

- [ ] **Step 1: Write the failing tests**

  In `composer.rs` tests:

```rust
#[test]
fn model_names_complete_across_providers() {
    let names = vec![
        ("opus".to_owned(), octet_core::Engine::CLAUDE),
        ("gpt-6-astra".to_owned(), octet_core::Engine::CODEX),
        ("gpt-5.6-sol".to_owned(), octet_core::Engine::CODEX),
    ];
    let tab = |text: &str| model_tab(text, text.len(), &names);
    assert!(matches!(tab("/model op"), Some(Tab::Replace { start: 7, ref text, popup: None }) if text == "opus "));
    assert!(matches!(
        tab("/model gpt"),
        Some(Tab::Replace { ref text, popup: Some(Completion { kind: Kind::Model, ref items, .. }), .. })
            if text == "gpt-" && items == &["gpt-6-astra · codex", "gpt-5.6-sol · codex"]
    ));
    assert!(tab("/mode op").is_none());
    assert!(tab("hello /model op").is_none());
    assert!(matches!(tab("/model zz"), Some(Tab::Nothing)));
    // Nothing typed: only the current provider's (first) names.
    assert!(matches!(tab("/model "), Some(Tab::Replace { ref text, popup: None, .. }) if text == "opus "));
}
```

  In `view/tests.rs`, add a merged-list test, and update the existing test
  that asserted `"claude catalog"` and `"256 entries"` to assert
  `"claude 256 (live)"` instead:

```rust
#[test]
fn the_model_list_merges_every_provider() {
    let c = octet_core::Config::new(octet_core::Engine::CLAUDE, "claude", "/tmp");
    let mut a = App::new(&c, "journal".into());
    let model = |s: &str, name: &str| octet_core::ModelInfo {
        selection: s.into(),
        id: Some(s.into()),
        name: name.into(),
        description: String::new(),
    };
    a.event(octet_core::Event::Models(vec![model("default", "Default"), model("opus", "Opus 5.5")]));
    a.models.catalogs.set(
        octet_core::Engine::CODEX,
        vec![model("gpt-6-astra", "GPT-6-Astra")],
        std::time::SystemTime::now() - std::time::Duration::from_secs(7200),
    );
    a.show_models(1);
    let text = a.entries_text();
    assert!(text.contains("claude 2 (live) · codex 1 (cached 2 h ago)"), "{text}");
    assert!(text.contains("Opus 5.5 · claude"), "{text}");
    assert!(text.contains("Select: /model opus"), "{text}");
    assert!(text.contains("GPT-6-Astra · codex"), "{text}");
    assert!(text.contains("Select: /model gpt-6-astra"), "{text}");
    // `default` names the current provider, so it is plain here.
    assert!(text.contains("Select: /model default"), "{text}");
    assert!(text.contains("switches provider when needed"), "{text}");
    assert!(!text.contains("starts fresh context"), "{text}");
    let claude = text.find("Opus 5.5 · claude").unwrap();
    let codex = text.find("GPT-6-Astra · codex").unwrap();
    assert!(claude < codex, "the current provider first");
}
```

  In `tests/keys.rs`, next to the existing completion-accept test, add a test
  that a `Kind::Model` popup item `"gpt-6-astra · codex"` accepted with Enter
  puts `/model gpt-6-astra ` in the draft:

```rust
#[test]
fn accepting_a_model_suggestion_inserts_only_the_name() {
    let mut app = crate::test_support::app_for(octet_core::Engine::CLAUDE);
    app.composer.editor.insert_str("/model gpt");
    app.composer.completion = Some(composer::Completion {
        kind: composer::Kind::Model,
        items: vec!["gpt-6-astra · codex".into()],
        selected: 0,
        start: 7,
    });
    crate::input::accept_completion(&mut app);
    assert_eq!(app.composer.editor.text(), "/model gpt-6-astra ");
}
```

  If the editor's insert method has another name, use the one the existing
  keys tests use.

- [ ] **Step 2: Run them to see the expected failure**

  Run: `scripts/rust-env.sh cargo test -p octet-tui --lib`

  Expected: compile failures (`model_tab`, `Kind::Model`), then assertion
  failures on the old list text.

- [ ] **Step 3: Implement**

  **(a)** In `composer.rs`, add `Model,` to `Kind`, and:

  ```rust
  /// Tab after `/model `: completes a model name from every provider's
  /// list. `None` when the draft is not a `/model` argument.
  pub(crate) fn model_tab(text: &str, cursor: usize, names: &[(String, octet_core::Engine)]) -> Option<Tab> {
      let start = word_start(text, cursor);
      if text[..start].trim_end() != "/model" {
          return None;
      }
      let word = &text[start..cursor];
      // Nothing typed yet: the current provider's names (they come first).
      let active = names.first().map(|(_, engine)| *engine);
      let matches: Vec<&(String, octet_core::Engine)> = names
          .iter()
          .filter(|(name, engine)| name.starts_with(word) && (!word.is_empty() || Some(*engine) == active))
          .collect();
      Some(match matches.as_slice() {
          [] => Tab::Nothing,
          [(name, _)] => Tab::Replace { start, text: format!("{name} "), popup: None },
          _ => {
              let plain: Vec<String> = matches.iter().map(|(name, _)| name.clone()).collect();
              let prefix = common_prefix(&plain);
              let popup = Completion {
                  kind: Kind::Model,
                  items: matches
                      .iter()
                      .take(crate::files::SHOWN)
                      .map(|(name, engine)| format!("{name} · {engine}"))
                      .collect(),
                  selected: 0,
                  start,
              };
              if prefix.len() > word.len() {
                  Tab::Replace { start, text: prefix, popup: Some(popup) }
              } else {
                  Tab::Popup(popup)
              }
          }
      })
  }
  ```

  **(b)** In `input.rs`, in the Tab handler, before the `off_loop` call:

  ```rust
  let names = app.models.names(app.conn.engine);
  if let Some(tab) = composer::model_tab(app.composer.editor.text(), app.composer.editor.cursor(), &names) {
      // apply `tab` exactly as the off-loop result is applied below
  }
  ```

  Factor the existing `match tab { Replace … Popup … Mention … Nothing … }`
  into a small `fn apply_tab(app: &mut App, tab: composer::Tab)`, so both
  paths share it. Return after applying the model `tab`.

  In `accept_completion`, add the arm:

  ```rust
  composer::Kind::Model => format!("{} ", item.split(" · ").next().unwrap_or(item)),
  ```

  **(c)** In `view.rs` `completion_popup`, give `Kind::Model` the title
  `" Models "`, matching the existing title strings' style.

  **(d)** In `app.rs`, rewrite `show_models` over one merged list:

  ```rust
  /// The current provider's models first (its live list, else its cache),
  /// then every other provider's cached list, in table order.
  fn model_entries(&self) -> Vec<(octet_core::Engine, octet_core::ModelInfo)> {
      let active = self.conn.engine;
      let first = if self.conn.models.is_empty() {
          self.models.catalogs.get(active).map(|l| l.models.clone()).unwrap_or_default()
      } else {
          self.conn.models.clone()
      };
      first
          .into_iter()
          .map(|m| (active, m))
          .chain(
              octet_core::Engine::ALL
                  .iter()
                  .copied()
                  .filter(|e| e.is_vendor() && *e != active)
                  .flat_map(|e| {
                      self.models
                          .catalogs
                          .get(e)
                          .map(|l| l.models.clone())
                          .unwrap_or_default()
                          .into_iter()
                          .map(move |m| (e, m))
                  }),
          )
          .collect()
  }

  /// `/model <selection>` when that resolves to `engine`, else the explicit form.
  fn select_line(&self, engine: octet_core::Engine, selection: &str) -> String {
      let plain = octet_core::model::Selection::resolve(selection, self.conn.engine, &self.models.catalogs)
          .is_ok_and(|r| r.selection.provider == engine);
      if plain { format!("/model {selection}") } else { format!("/model {engine} {selection}") }
  }
  ```

  In `show_models`:
  - Use `let entries = self.model_entries();` for the page count and the
    slice, in place of `self.conn.models`.
  - Build the header from every vendor provider, the current one first:
    `"{engine} {count} ({freshness})"` joined by `" · "`. Take the freshness
    from `self.models.freshness(engine, engine == self.conn.engine && !self.conn.models.is_empty(), now)`.
    Then `" · page {page}/{pages}. PgUp/PgDn scroll; /model list <page>. Account access may vary."`.
  - Show each entry as
    `"{name} · {engine}\nModel ID: {id}\n{description}\nSelect: {select_line}"`.
  - When `entries` is empty, show the note "No model list yet. Explicit model IDs remain supported; /model refresh fetches the lists."
  - Change the footer's second line to
    `"/model <name> switches provider when needed; the conversation goes with you. /model refresh fetches the lists again."`.
  - Compute `catalog_focus` from `entries.len()`.

- [ ] **Step 4: Run the tests and see them pass**

  Run: `scripts/rust-env.sh cargo test -p octet-tui --lib`

  Expected: all pass, including the updated view test.

- [ ] **Step 5: Gate and commit**

  Run: `make rust-check` (expected: exit 0), then:

  ```bash
  git add crates/octet-tui/src/app.rs crates/octet-tui/src/composer.rs crates/octet-tui/src/input.rs \
    crates/octet-tui/src/view.rs crates/octet-tui/src/view/tests.rs crates/octet-tui/src/tests/keys.rs
  git commit -m "TUI: /model lists every provider's models; Tab completes them

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

### Task 6: Docs and the live check

**Files:**

- Modify: `docs/tui.md` (the "Switch models and providers" section, and
  "Model discovery"), `README.md` (Models and providers), `CHANGELOG.md`
  (Added), `docs/rust/tui-features.md` (the model rows)

- [ ] **Step 1: Docs.** State that:
  - `/model` lists every provider's models: the current one live, the others
    from `models.json`, fetched by starting their CLI catalog-only (no
    session, no tokens) when missing or older than 24 hours;
  - `/model refresh` fetches them again;
  - a bare `/model NAME` goes to the provider whose list holds it, else by
    the leading word its listed IDs share, else the current provider;
  - `default` is the current provider's;
  - Tab completes model names after `/model `;
  - the explicit forms are unchanged.

  Remove "Switching providers starts fresh context" wherever it remains.

- [ ] **Step 2: Live check** (release build, tmux, real CLIs, a throwaway
  `--cwd` and `--journal-dir`)
  1. Record the count of `~/.codex/sessions/**/*.jsonl`.
  2. Start `octet --engine claude`. Wait for `● ready`, then wait until
     `models.json` holds a `codex` entry; record the time.
  3. Record the count of Codex session files again; it must equal step 1's.
  4. Send "Remember the code OCTET_LIST_7. Reply with only the code." Then
     `/model list`: the screen shows both providers with `live` and
     `cached just now`.
  5. Send `/model gpt-6-astra` (or another model from Codex's list). The
     note says it is in Codex's list, and the session switches to Codex.
     Ask for the code: the reply contains `OCTET_LIST_7`.
  6. Send `/model opus`. It switches to Claude; ask again for the same
     answer.
  7. Quit, and start again with the same `--journal-dir`. For 5 s after
     `● ready`, Octet's process tree holds a single vendor CLI (no probe).

  Record the results in the commit message.
- [ ] **Step 3: Gate and commit**

  Run: `make rust-check` (expected: exit 0), then:

  ```bash
  git add docs/tui.md README.md CHANGELOG.md docs/rust/tui-features.md
  git commit -m "Docs: one model list across providers

  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
  ```

## Finish

- A final whole-branch review by a fresh reviewer on the most capable model,
  then one fix pass.
- Merge only when the user says.
