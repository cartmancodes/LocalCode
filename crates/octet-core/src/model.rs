//! Model names remain vendor-owned; no stale hard-coded model catalog.
use crate::{Config, Engine, SelectionError};
use std::path::PathBuf;
#[derive(Debug, Clone, PartialEq, Eq)]
/// A `/model` choice: which provider, and which of its models.
pub struct Selection {
    /// Claude or Codex; `parse` never yields `Demo`.
    pub provider: Engine,
    /// The model name; `None` uses the provider's default.
    pub model: Option<String>,
}
impl Selection {
    /// Parses a `/model` argument; a bare model name keeps the `current`
    /// provider.
    ///
    /// ```
    /// use octet_core::{model::Selection, Engine};
    ///
    /// let both = Selection::parse("claude sonnet", Engine::CODEX).unwrap();
    /// assert_eq!(both, Selection::parse("claude/sonnet", Engine::CODEX).unwrap());
    /// assert_eq!(Selection::parse("codex", Engine::CLAUDE).unwrap().model, None);
    /// ```
    /// # Errors
    ///
    /// Fails for empty or malformed input, the demo engine, or an invalid
    /// model name.
    pub fn parse(input: &str, current: Engine) -> Result<Self, SelectionError> {
        let vendor = |name: &str| Engine::parse(name).filter(|engine| engine.is_vendor());
        let parts: Vec<_> = input.split_whitespace().collect();
        let (provider, model) = match parts.as_slice() {
            [] => return Err(SelectionError::Empty),
            [name] => match vendor(name) {
                Some(engine) => (engine, "default"),
                None => name
                    .split_once('/')
                    .and_then(|(prefix, model)| Some((vendor(prefix)?, model)))
                    .unwrap_or((current, *name)),
            },
            [name, model] => match vendor(name) {
                Some(engine) => (engine, *model),
                None => return Err(SelectionError::Syntax),
            },
            _ => return Err(SelectionError::Syntax),
        };
        if !provider.is_vendor() {
            return Err(SelectionError::Demo);
        }
        if !octet_engine::live::valid_identifier(model) {
            return Err(SelectionError::InvalidModel);
        }
        Ok(Self {
            provider,
            model: (model != "default").then(|| model.into()),
        })
    }
    /// The configuration for the next connection: the same provider resumes
    /// `session` and keeps its binary; another provider starts fresh.
    pub fn configure(&self, current: &Config, session: &str, binary: Option<PathBuf>) -> Config {
        let same = self.provider == current.engine;
        Config {
            mode: current.mode,
            approval_timeout: current.approval_timeout,
            effort: current.effort.clone(),
            engine: self.provider,
            model: self.model.clone(),
            cwd: current.cwd.clone(),
            binary: binary.unwrap_or_else(|| {
                if same {
                    current.binary.clone()
                } else {
                    PathBuf::from(self.provider.provider().default_binary)
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
/// The providers `/model` can select, from the provider table: every vendor,
/// not the offline demo.
pub fn vendor_names() -> Vec<&'static str> {
    Engine::ALL
        .iter()
        .filter(|engine| engine.is_vendor())
        .map(|engine| engine.as_str())
        .collect()
}

/// "a, b or c".
pub fn or_list<S: AsRef<str>>(items: &[S]) -> String {
    match items {
        [] => String::new(),
        [only] => only.as_ref().to_owned(),
        [rest @ .., last] => {
            let rest: Vec<&str> = rest.iter().map(AsRef::as_ref).collect();
            format!("{} or {}", rest.join(", "), last.as_ref())
        }
    }
}

/// `/model` with no argument.
pub(crate) fn usage_empty(vendors: &[&str]) -> String {
    let forms: Vec<String> = vendors
        .iter()
        .map(|v| format!("/model {v} <name>"))
        .collect();
    format!(
        "Use /model <name>, {}, or /model <provider> default",
        forms.join(", ")
    )
}

/// `/model` with words that do not form a selection.
pub(crate) fn usage_syntax(vendors: &[&str]) -> String {
    format!("Use /model <name> or /model <{}> <name>", vendors.join("|"))
}

/// `/model` asked of the offline demo.
pub(crate) fn usage_demo(vendors: &[&str]) -> String {
    let forms: Vec<String> = vendors.iter().map(|v| format!("/model {v}")).collect();
    format!(
        "Choose a real provider: {}. Demo has no model.",
        or_list(&forms)
    )
}

/// The catalog footer's first line.
pub fn catalog_hint(vendors: &[&str]) -> String {
    std::iter::once("/model <ID or alias>".to_owned())
        .chain(vendors.iter().map(|v| format!("/model {v} <ID>")))
        .collect::<Vec<_>>()
        .join(" · ")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn usage_text_names_every_vendor() {
        let three = ["codex", "claude", "gemini"];
        assert_eq!(or_list(&["a"]), "a");
        assert_eq!(or_list(&["a", "b", "c"]), "a, b or c");
        assert_eq!(
            usage_empty(&three),
            "Use /model <name>, /model codex <name>, /model claude <name>, /model gemini <name>, or /model <provider> default"
        );
        assert_eq!(
            usage_syntax(&three),
            "Use /model <name> or /model <codex|claude|gemini> <name>"
        );
        assert_eq!(
            usage_demo(&three),
            "Choose a real provider: /model codex, /model claude or /model gemini. Demo has no model."
        );
        assert_eq!(
            catalog_hint(&three),
            "/model <ID or alias> · /model codex <ID> · /model claude <ID> · /model gemini <ID>"
        );
        assert_eq!(vendor_names(), ["codex", "claude"]);
    }
    fn config() -> Config {
        Config {
            model: Some("old".into()),
            resume: Some("old-thread".into()),
            mode: crate::Mode::Auto,
            ..Config::new(Engine::CODEX, "/custom/codex", "/workspace")
        }
    }
    #[test]
    fn configure_carries_approval_timeout() {
        let current = Config {
            approval_timeout: std::time::Duration::from_secs(300),
            ..config()
        };
        let next = Selection::parse("claude example", Engine::CODEX)
            .unwrap()
            .configure(&current, "thread", None);
        assert_eq!(next.approval_timeout, std::time::Duration::from_secs(300));
    }
    #[test]
    fn configure_carries_mode() {
        let selection = Selection::parse("claude example", Engine::CODEX).unwrap();
        assert_eq!(
            selection.configure(&config(), "thread", None).mode,
            crate::Mode::Auto
        );
    }
    #[test]
    fn parses_provider_model_forms_without_restricting_vendor_names() {
        assert_eq!(
            Selection::parse("claude example-model", Engine::CODEX).unwrap(),
            Selection::parse("claude/example-model", Engine::CODEX).unwrap()
        );
        assert_eq!(
            Selection::parse("codex", Engine::CLAUDE).unwrap().model,
            None
        );
        assert_eq!(
            Selection::parse("custom/name", Engine::CODEX)
                .unwrap()
                .model
                .as_deref(),
            Some("custom/name")
        );
        for bad in ["", "claude/", "other model", "claude model extra"] {
            assert!(Selection::parse(bad, Engine::CODEX).is_err());
        }
        assert!(Selection::parse("example", Engine::DEMO).is_err());
    }
    #[test]
    fn same_provider_retains_context_and_custom_binary() {
        let selected = Selection::parse("new-model", Engine::CODEX)
            .unwrap()
            .configure(&config(), "live-thread", None);
        assert_eq!(selected.resume.as_deref(), Some("live-thread"));
        assert_eq!(selected.binary, PathBuf::from("/custom/codex"));
        assert_eq!(selected.model.as_deref(), Some("new-model"));
    }
    #[test]
    fn changing_provider_cannot_reuse_foreign_session_or_binary() {
        let selected = Selection::parse("claude default", Engine::CODEX)
            .unwrap()
            .configure(&config(), "live-thread", None);
        assert_eq!(selected.resume, None);
        assert_eq!(selected.binary, PathBuf::from("claude"));
        assert_eq!(selected.cwd, PathBuf::from("/workspace"));
    }
}
