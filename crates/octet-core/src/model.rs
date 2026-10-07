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
            // A level the new provider does not take falls back to its default.
            effort: current
                .effort
                .clone()
                .filter(|level| self.provider.check_effort(level).is_ok()),
            // A fork the vendor has not named yet stays a fork; resuming
            // the original instead would write into it.
            fork: same && session.is_empty() && current.fork,
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

/// Every engine's name, as "a, b or c", for the CLI's usage error.
pub fn engine_choices() -> String {
    let names: Vec<&str> = Engine::ALL.iter().map(|engine| engine.as_str()).collect();
    or_list(&names)
}

/// "a, b or c".
fn or_list<S: AsRef<str>>(items: &[S]) -> String {
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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn configure_drops_an_effort_the_new_provider_does_not_take() {
        let mut current = Config::new(Engine::CODEX, "codex", "/tmp");
        current.effort = Some("minimal".into());
        let claude = Selection {
            provider: Engine::CLAUDE,
            model: None,
        };
        assert_eq!(claude.configure(&current, "t-1", None).effort, None);
        current.effort = Some("high".into());
        assert_eq!(
            claude.configure(&current, "t-1", None).effort.as_deref(),
            Some("high")
        );
    }
    #[test]
    fn configure_keeps_a_fork_the_vendor_has_not_named() {
        let mut current = Config::new(Engine::CLAUDE, "claude", "/tmp");
        current.resume = Some("original".into());
        current.fork = true;
        let selection = Selection {
            provider: Engine::CLAUDE,
            model: Some("opus".into()),
        };
        let next = selection.configure(&current, "", None);
        assert_eq!(next.resume.as_deref(), Some("original"));
        assert!(
            next.fork,
            "the fork must not turn into a resume of the original"
        );
        let named = selection.configure(&current, "forked", None);
        assert_eq!(named.resume.as_deref(), Some("forked"));
        assert!(!named.fork);
    }
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
