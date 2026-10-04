//! Model names remain vendor-owned; no stale hard-coded model catalog.
use crate::{Config, Engine};
use std::path::PathBuf;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Selection {
    /// Claude or Codex; `parse` never yields `Demo`.
    pub provider: Engine,
    pub model: Option<String>,
}
impl Selection {
    pub fn parse(input: &str, current: Engine) -> Result<Self, String> {
        let vendor = |name: &str| Engine::parse(name).filter(|engine| engine.is_vendor());
        let parts: Vec<_> = input.split_whitespace().collect();
        let (provider, model) = match parts.as_slice() {
            [] => return Err("Use /model <name>, /model codex <name>, /model claude <name>, or /model <provider> default".into()),
            [name] => match vendor(name) {
                Some(engine) => (engine, "default"),
                None => name
                    .split_once('/')
                    .and_then(|(prefix, model)| Some((vendor(prefix)?, model)))
                    .unwrap_or((current, *name)),
            },
            [name, model] => match vendor(name) {
                Some(engine) => (engine, *model),
                None => return Err("Use /model <name> or /model <codex|claude> <name>".into()),
            },
            _ => return Err("Use /model <name> or /model <codex|claude> <name>".into()),
        };
        if !provider.is_vendor() {
            return Err(
                "Choose a real provider: /model codex or /model claude. Demo has no model.".into(),
            );
        }
        if !octet_engine::live::valid_identifier(model) {
            return Err("Model must be a non-empty vendor model name, at most 256 bytes".into());
        }
        Ok(Self {
            provider,
            model: (model != "default").then(|| model.into()),
        })
    }
    pub fn configure(&self, current: &Config, session: &str, binary: Option<PathBuf>) -> Config {
        let same = self.provider == current.engine;
        Config {
            mode: current.mode,
            engine: self.provider,
            model: self.model.clone(),
            cwd: current.cwd.clone(),
            binary: binary.unwrap_or_else(|| {
                if same {
                    current.binary.clone()
                } else {
                    PathBuf::from(self.provider.as_str())
                }
            }),
            resume: if same {
                if session.is_empty() {
                    current.resume.clone()
                } else {
                    Some(session.into())
                }
            } else {
                None
            },
        }
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {
        Config {
            model: Some("old".into()),
            resume: Some("old-thread".into()),
            mode: crate::Mode::Auto,
            ..Config::new(Engine::Codex, "/custom/codex", "/workspace")
        }
    }
    #[test]
    fn configure_carries_mode() {
        let selection = Selection::parse("claude example", Engine::Codex).unwrap();
        assert_eq!(
            selection.configure(&config(), "thread", None).mode,
            crate::Mode::Auto
        );
    }
    #[test]
    fn parses_provider_model_forms_without_restricting_vendor_names() {
        assert_eq!(
            Selection::parse("claude example-model", Engine::Codex).unwrap(),
            Selection::parse("claude/example-model", Engine::Codex).unwrap()
        );
        assert_eq!(
            Selection::parse("codex", Engine::Claude).unwrap().model,
            None
        );
        assert_eq!(
            Selection::parse("custom/name", Engine::Codex)
                .unwrap()
                .model
                .as_deref(),
            Some("custom/name")
        );
        for bad in ["", "claude/", "other model", "claude model extra"] {
            assert!(Selection::parse(bad, Engine::Codex).is_err());
        }
        assert!(Selection::parse("example", Engine::Demo).is_err());
    }
    #[test]
    fn same_provider_retains_context_and_custom_binary() {
        let selected = Selection::parse("new-model", Engine::Codex)
            .unwrap()
            .configure(&config(), "live-thread", None);
        assert_eq!(selected.resume.as_deref(), Some("live-thread"));
        assert_eq!(selected.binary, PathBuf::from("/custom/codex"));
        assert_eq!(selected.model.as_deref(), Some("new-model"));
    }
    #[test]
    fn changing_provider_cannot_reuse_foreign_session_or_binary() {
        let selected = Selection::parse("claude default", Engine::Codex)
            .unwrap()
            .configure(&config(), "live-thread", None);
        assert_eq!(selected.resume, None);
        assert_eq!(selected.binary, PathBuf::from("claude"));
        assert_eq!(selected.cwd, PathBuf::from("/workspace"));
    }
}
