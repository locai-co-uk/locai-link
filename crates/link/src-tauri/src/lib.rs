// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Locai Link: one binary in two shapes.
//! - headless (`--no-default-features`): the `supervisor` loop only.
//! - desktop (`ui`, default): the supervisor plus the tray and setup/preferences windows.

pub mod supervisor;

// Shape-agnostic device lifecycle (deregister / start / stop / restart / uninstall).
pub mod lifecycle;

// Folded-in platform/Control helpers.
mod shared;

#[cfg(feature = "ui")]
mod preferences;
#[cfg(feature = "ui")]
mod setup;
#[cfg(feature = "ui")]
mod tray;

#[cfg(feature = "ui")]
pub use tray::run;
