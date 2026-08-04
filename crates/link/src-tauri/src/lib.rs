// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

//! Locai Link — one binary, two shapes:
//! - headless (`--no-default-features`): the `supervisor` loop only (resolve
//!   `current`, spawn + supervise the runtime child, exit-42 respawn, rollback,
//!   Pattern-B bootstrap).
//! - desktop (`ui`, default): the supervisor plus the tray + setup/preferences
//!   windows (the `tray` / `preferences` / `setup` modules).

pub mod supervisor;

// Folded-in platform/Control helpers (was the `locai-link-shared` crate).
mod shared;

#[cfg(feature = "ui")]
mod preferences;
#[cfg(feature = "ui")]
mod setup;
#[cfg(feature = "ui")]
mod tray;

#[cfg(feature = "ui")]
pub use tray::run;
