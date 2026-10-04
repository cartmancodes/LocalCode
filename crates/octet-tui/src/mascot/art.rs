//! Pixel grids of the nine mini Octet poses: 9 × 6 pixels each, one
//! character per pixel, `.` transparent; the other characters are `PALETTE`
//! keys in `mascot.rs`. Only the eyes and a small accent change between
//! poses. The full pixel-art reference lives in `docs/design/octet-agent-modes/`.
use super::State;

pub(super) fn mini(state: State) -> &'static [&'static str; 6] {
    match state {
        State::Idle => &MINI_IDLE,
        State::Thinking => &MINI_THINKING,
        State::Coding => &MINI_CODING,
        State::Searching => &MINI_SEARCHING,
        State::Delegating => &MINI_DELEGATING,
        State::Approval => &MINI_APPROVAL,
        State::Success => &MINI_SUCCESS,
        State::Error => &MINI_ERROR,
        State::Sleeping => &MINI_SLEEPING,
    }
}

#[rustfmt::skip]
const MINI_IDLE: [&str; 6] = [
    "..oorrr..",
    ".orrrrrr.",
    ".rcKrcKr.",
    ".rrrrrrr.",
    "orrdrdrro",
    "o.o.o.o.o",
];

#[rustfmt::skip]
const MINI_THINKING: [&str; 6] = [
    "..oorrr.c",
    ".orrrrrr.",
    ".rcKrcKr.",
    ".rrrrrrr.",
    "orrdrdrro",
    "o.o.o.o.o",
];

#[rustfmt::skip]
const MINI_CODING: [&str; 6] = [
    "..oorrr..",
    ".orrrrrr.",
    ".rrrrrrr.",
    ".rcKrcKr.",
    "orrdrdrro",
    "o.o.o.o.o",
];

#[rustfmt::skip]
const MINI_SEARCHING: [&str; 6] = [
    "..oorrr..",
    ".orrrrrr.",
    ".rKcrKcr.",
    ".rrrrrrr.",
    "orrdrdrro",
    "o.o.o.o.o",
];

#[rustfmt::skip]
const MINI_DELEGATING: [&str; 6] = [
    "..oorrr..",
    ".orrrrrr.",
    ".rcKrcKr.",
    ".rrrrrrr.",
    "orrdrdrrc",
    "o.o.o.o.c",
];

#[rustfmt::skip]
const MINI_APPROVAL: [&str; 6] = [
    "..oorrr..",
    ".orrrrrr.",
    ".rcKrcKr.",
    ".rrrrrrr.",
    "orrdrdrcc",
    "o.o.o.occ",
];

#[rustfmt::skip]
const MINI_SUCCESS: [&str; 6] = [
    "c.oorrr.c",
    ".orrrrrr.",
    ".rKKrKKr.",
    ".KrrrrrK.",
    "orrdrdrro",
    "o.o.o.o.o",
];

#[rustfmt::skip]
const MINI_ERROR: [&str; 6] = [
    "..oorrr..",
    ".orrrrrr.",
    ".rKcrcKr.",
    ".rcKrKcr.",
    "orrdrdrro",
    "o.o.o.o.o",
];

#[rustfmt::skip]
const MINI_SLEEPING: [&str; 6] = [
    "..oorrr.c",
    ".orrrrrrc",
    ".rrrrrrr.",
    ".rKKrKKr.",
    "orrdrdrro",
    "o.o.o.o.o",
];
