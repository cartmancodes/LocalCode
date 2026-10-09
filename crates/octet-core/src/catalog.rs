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
pub const STALE_AFTER: Duration = Duration::from_hours(24);
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
            // A time the clock cannot hold is a bad entry, not a crash.
            let Some(fetched) = entry["fetched"]
                .as_u64()
                .and_then(|seconds| UNIX_EPOCH.checked_add(Duration::from_secs(seconds)))
            else {
                continue;
            };
            let models = entry["models"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(model_from_json)
                .take(MAX_MODELS)
                .collect();
            lists.insert(engine, Listed { models, fetched });
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
            root.insert(
                engine.as_str().into(),
                json!({"fetched": seconds, "models": models}),
            );
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
        self.lists.insert(
            engine,
            Listed {
                models,
                fetched: at,
            },
        );
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
    Engine::ALL
        .iter()
        .copied()
        .filter(|engine| engine.is_vendor())
}

/// A cached model, if it is a usable one.
fn model_from_json(value: &Value) -> Option<ModelInfo> {
    let valid = |s: &&str| octet_engine::live::valid_identifier(s);
    let selection = value["selection"].as_str().filter(valid)?;
    let text = |key: &str, limit: usize| {
        value[key]
            .as_str()
            .filter(|s| s.len() <= limit && !s.chars().any(char::is_control))
            .unwrap_or_default()
            .to_owned()
    };
    Some(ModelInfo {
        selection: selection.to_owned(),
        id: value["id"].as_str().filter(valid).map(str::to_owned),
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

#[cfg(test)]
mod tests {
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
        c.set(
            Engine::CLAUDE,
            vec![
                model("default", Some("claude-fable-5-1")),
                model("opus", Some("claude-opus-5-5")),
            ],
            at,
        );
        c.set(
            Engine::CODEX,
            vec![
                model("gpt-6-astra", Some("gpt-6-astra")),
                model("gpt-5.6-sol", Some("gpt-5.6-sol")),
            ],
            at,
        );
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
        assert!(
            c.prefix_owners("opus").is_empty(),
            "a name without a leading word"
        );
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
        use std::os::unix::fs::PermissionsExt;
        let dir = octet_testkit::TempDir::new("octet-catalog");
        std::fs::create_dir_all(dir.path()).unwrap();
        let path = dir.path().join(FILE);
        sample().save(&path).unwrap();
        assert_eq!(Catalogs::load(&path), sample());
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
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
    fn a_time_past_the_clock_is_skipped() {
        let dir = octet_testkit::TempDir::new("octet-catalog-far");
        std::fs::create_dir_all(dir.path()).unwrap();
        let path = dir.path().join(FILE);
        std::fs::write(
            &path,
            r#"{"codex":{"fetched":18446744073709551615,"models":[{"selection":"gpt-ok"}]}}"#,
        )
        .unwrap();
        assert!(Catalogs::load(&path).get(Engine::CODEX).is_none());
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
}
