//! Model names remain vendor-owned; no stale hard-coded model catalog.
use crate::Config;
use std::path::PathBuf;
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Selection {
    pub provider: String,
    pub model: Option<String>,
}
impl Selection {
    pub fn parse(input: &str, current: &str) -> Result<Self, String> {
        let parts: Vec<_> = input.split_whitespace().collect();
        let (provider,model)=match parts.as_slice(){
            []=>return Err("Use /model <name>, /model codex <name>, /model claude <name>, or /model <provider> default".into()),
            [provider @ ("codex"|"claude")] => (*provider,"default"),
            [name] => {
                if let Some((provider,model))=name.split_once('/') {
                    if matches!(provider,"codex"|"claude") {(provider,model)}else{(current,*name)}
                }else{(current,*name)}
            },
            [provider,name] if matches!(*provider,"codex"|"claude") => (*provider,*name),
            _=>return Err("Use /model <name> or /model <codex|claude> <name>".into()),
        };
        if !matches!(provider, "codex" | "claude") {
            return Err(
                "Choose a real provider: /model codex or /model claude. Demo has no model.".into(),
            );
        }
        if model.is_empty() || model.len() > 256 || model.chars().any(char::is_control) {
            return Err("Model must be a non-empty vendor model name, at most 256 bytes".into());
        }
        Ok(Self {
            provider: provider.into(),
            model: if model == "default" {
                None
            } else {
                Some(model.into())
            },
        })
    }
    pub fn configure(&self, current: &Config, session: &str, binary: Option<PathBuf>) -> Config {
        let same = self.provider == current.engine;
        Config {
            engine: self.provider.clone(),
            model: self.model.clone(),
            cwd: current.cwd.clone(),
            binary: binary.unwrap_or_else(|| {
                if same {
                    current.binary.clone()
                } else {
                    PathBuf::from(&self.provider)
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
            engine: "codex".into(),
            binary: "/custom/codex".into(),
            cwd: "/workspace".into(),
            model: Some("old".into()),
            resume: Some("old-thread".into()),
        }
    }
    #[test]
    fn parses_provider_model_forms_without_restricting_vendor_names() {
        assert_eq!(
            Selection::parse("claude example-model", "codex").unwrap(),
            Selection::parse("claude/example-model", "codex").unwrap()
        );
        assert_eq!(Selection::parse("codex", "claude").unwrap().model, None);
        assert_eq!(
            Selection::parse("custom/name", "codex")
                .unwrap()
                .model
                .as_deref(),
            Some("custom/name")
        );
        for bad in ["", "claude/", "other model", "claude model extra"] {
            assert!(Selection::parse(bad, "codex").is_err());
        }
        assert!(Selection::parse("example", "demo").is_err());
    }
    #[test]
    fn same_provider_retains_context_and_custom_binary() {
        let selected = Selection::parse("new-model", "codex").unwrap().configure(
            &config(),
            "live-thread",
            None,
        );
        assert_eq!(selected.resume.as_deref(), Some("live-thread"));
        assert_eq!(selected.binary, PathBuf::from("/custom/codex"));
        assert_eq!(selected.model.as_deref(), Some("new-model"));
    }
    #[test]
    fn changing_provider_cannot_reuse_foreign_session_or_binary() {
        let selected = Selection::parse("claude default", "codex")
            .unwrap()
            .configure(&config(), "live-thread", None);
        assert_eq!(selected.resume, None);
        assert_eq!(selected.binary, PathBuf::from("claude"));
        assert_eq!(selected.cwd, PathBuf::from("/workspace"));
    }
}
