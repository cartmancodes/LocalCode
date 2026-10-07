//! The release workflow's shell steps, run against a fake `gh`.
#![cfg(unix)]
#![expect(
    clippy::unwrap_used,
    reason = "test code: an unwrap that fails is the test failing"
)]
use std::{path::Path, process::Command};

fn script(name: &str) -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../scripts")
        .join(name)
}

fn folder(prefix: &str) -> octet_testkit::TempDir {
    let dir = octet_testkit::TempDir::new(prefix);
    std::fs::create_dir_all(dir.path()).unwrap();
    dir
}

#[test]
fn the_tag_must_name_the_crate_version() {
    let dir = folder("octet-release-tag");
    let manifest = dir.path().join("Cargo.toml");
    std::fs::write(
        &manifest,
        "[package]\nname = \"octet\"\nversion = \"0.2.0-rc1\"\n",
    )
    .unwrap();
    let check = |tag: &str| {
        Command::new(script("release-tag-check.sh"))
            .args([tag, manifest.to_str().unwrap()])
            .output()
            .unwrap()
    };
    assert!(check("v0.2.0-rc1").status.success());
    let wrong = check("v0.2.0");
    assert!(!wrong.status.success());
    assert!(String::from_utf8_lossy(&wrong.stderr).contains("does not match"));
}

/// Runs the publish script for `tag` with a fake `gh` whose `release view`
/// exits `view_exit`; the `gh` calls it made.
fn publish(tag: &str, view_exit: i32) -> Vec<String> {
    let dir = folder("octet-release-publish");
    let bin = dir.path().join("bin");
    let dist = dir.path().join("dist");
    std::fs::create_dir_all(&bin).unwrap();
    std::fs::create_dir_all(&dist).unwrap();
    std::fs::write(dist.join("octet.tar.gz"), "x").unwrap();
    let log = dir.path().join("gh.log");
    octet_testkit::write_script(
        &bin,
        "gh",
        "echo \"$*\" >> \"$GH_LOG\"\n\
         if [ \"$1 $2\" = \"release view\" ]; then exit \"$GH_VIEW_EXIT\"; fi",
    );
    let path = octet_testkit::path_with(&bin);
    let status = Command::new(script("release-publish.sh"))
        .args([tag, dist.to_str().unwrap()])
        .env("PATH", path)
        .env("GH_LOG", &log)
        .env("GH_VIEW_EXIT", view_exit.to_string())
        .env("GITHUB_REPOSITORY", "owner/octet")
        .status()
        .unwrap();
    assert!(status.success());
    std::fs::read_to_string(log)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect()
}

#[test]
fn a_new_release_is_created_and_a_dash_makes_it_a_pre_release() {
    let calls = publish("v0.2.0", 1);
    assert!(calls[1].starts_with("release create v0.2.0 "), "{calls:?}");
    assert!(!calls[1].contains("--prerelease"));
    let calls = publish("v0.2.0-rc1", 1);
    assert!(calls[1].ends_with("--prerelease"), "{calls:?}");
}

#[test]
fn a_rerun_replaces_the_files_of_an_existing_release() {
    let calls = publish("v0.2.0", 0);
    assert_eq!(calls.len(), 2, "{calls:?}");
    assert!(calls[1].starts_with("release upload v0.2.0 "));
    assert!(calls[1].ends_with("--clobber"));
}
