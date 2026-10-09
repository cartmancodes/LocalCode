//! Every provider's model list for `/model`: the cache, the probes that
//! refresh it, and how fresh each list is. Nothing here names a vendor.
use octet_core::{
    Engine, ModelInfo,
    catalog::{self, Catalogs},
};
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
    /// probe of it failed this run. The offline demo starts no CLI: there,
    /// only `/model refresh` fetches.
    pub(crate) fn probe_stale(&mut self, active: Engine, now: SystemTime) {
        if !active.is_vendor() {
            return;
        }
        for engine in others(active) {
            if self.catalogs.stale(engine, now) && !self.failed.contains_key(&engine) {
                self.probe(engine);
            }
        }
    }

    /// `/model refresh`: probes every other provider now. The providers it
    /// started probing.
    pub(crate) fn refresh(&mut self, active: Engine) -> Vec<Engine> {
        others(active)
            .filter(|engine| {
                self.failed.remove(engine);
                self.probe(*engine)
            })
            .collect()
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
            let binary = (prober.binary)(engine);
            let result = octet_core::probe(engine, binary, prober.cwd, prober.limit).await;
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
    pub(crate) fn live(
        &mut self,
        engine: Engine,
        models: Vec<ModelInfo>,
        now: SystemTime,
    ) -> Option<String> {
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
            (None, Some(error)) => {
                format!("unavailable: {}", error.lines().next().unwrap_or_default())
            }
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
                self.catalogs
                    .get(engine)
                    .into_iter()
                    .flat_map(move |listed| {
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
    Engine::ALL
        .iter()
        .copied()
        .filter(move |engine| engine.is_vendor() && *engine != active)
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

#[cfg(test)]
mod tests {
    use super::*;
    use octet_core::Engine;
    use std::time::{Duration, SystemTime};

    fn model(s: &str) -> ModelInfo {
        ModelInfo {
            selection: s.into(),
            id: Some(s.into()),
            name: s.into(),
            description: String::new(),
        }
    }
    fn attached(dir: &Path) -> Models {
        let mut models = Models::default();
        models.attach(
            dir,
            Prober {
                cwd: std::env::temp_dir(),
                binary: |_| octet_testkit::protocol_child(),
                limit: octet_core::PROBE_LIMIT,
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
        assert!(
            cached
                .get(Engine::CODEX)
                .is_some_and(|l| !l.models.is_empty())
        );
        // Fresh now: no probe.
        models.probe_stale(Engine::CLAUDE, SystemTime::now());
        assert!(models.probing.is_empty());
    }

    #[tokio::test]
    async fn the_offline_demo_probes_nothing() {
        let dir = octet_testkit::TempDir::new("octet-models-demo");
        std::fs::create_dir_all(dir.path()).unwrap();
        let mut models = attached(dir.path());
        models.probe_stale(Engine::DEMO, SystemTime::now());
        assert!(models.probing.is_empty(), "{:?}", models.probing);
    }

    #[test]
    fn a_failed_provider_is_not_probed_again_until_refresh() {
        let mut models = Models::default();
        models.failed.insert(Engine::CODEX, "not signed in".into());
        assert!(
            models
                .freshness(Engine::CODEX, false, SystemTime::now())
                .starts_with("unavailable: not signed in")
        );
        models.probe_stale(Engine::CLAUDE, SystemTime::now());
        assert!(models.probing.is_empty());
    }

    #[test]
    fn freshness_says_where_a_list_came_from() {
        let mut models = Models::default();
        let now = SystemTime::now();
        assert_eq!(models.freshness(Engine::CODEX, false, now), "not fetched");
        models.catalogs.set(
            Engine::CODEX,
            vec![model("a-1")],
            now - Duration::from_hours(2),
        );
        assert_eq!(
            models.freshness(Engine::CODEX, false, now),
            "cached 2 h ago"
        );
        assert_eq!(models.freshness(Engine::CODEX, true, now), "live");
    }

    #[test]
    fn names_put_the_active_provider_first_once_each() {
        let mut models = Models::default();
        let now = SystemTime::now();
        models
            .catalogs
            .set(Engine::CODEX, vec![model("gpt-6-astra")], now);
        models
            .catalogs
            .set(Engine::CLAUDE, vec![model("opus"), model("opus")], now);
        let names = models.names(Engine::CLAUDE);
        assert_eq!(
            names,
            [
                ("opus".into(), Engine::CLAUDE),
                ("gpt-6-astra".into(), Engine::CODEX)
            ]
        );
    }
}
