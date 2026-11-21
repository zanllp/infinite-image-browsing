# Desktop updates

The Windows installers (Nuitka and PyInstaller) and the macOS apps (Intel and
Apple silicon) each read their own feed from the latest public GitHub Release.
Drafts are not offered. The WebUI extension and standalone server do not check
for app updates. An older desktop app without the updater must be upgraded
manually once. On macOS, install the app in a writable location, not on the DMG.

## One-time release setup

1. Generate a Tauri v1 signing key **outside the checkout**, using the CLI
   installed in `vue`: `node_modules/.bin/tauri signer generate -w ~/.tauri/iib-update.key`.
   Back up the key and password. Keep using the same key for future releases.
2. In GitHub Actions, set the repository **variable** `IIB_UPDATER_PUBLIC_KEY`
   to the contents of the generated `.pub` file.
3. Set the Actions **secret** `IIB_UPDATER_PRIVATE_KEY` to the contents of the
   private key file, and `IIB_UPDATER_KEY_PASSWORD` to its password (omit if empty).
   These are updater keys, not Apple or Windows code-signing certificates.

## Windows verification without a release

Dispatch `tauri_app_build.yml` on the feature branch with `mode=verify-windows`.
This checks both NSIS variants on Windows runners, including signing and
unattended install/relaunch. The build jobs have read-only repository access;
they upload verification logs, not signed installers. No tag or draft is created.

## Each release

Bump the desktop version before building. The existing desktop workflow tests
the installers, archives the same NSIS exe or the final signed macOS app, signs
the archive, and attaches the archive, signature and per-channel JSON to the
draft. The signing key is only supplied after the frontend and app builds.
Publish the draft as the latest stable release once all four feeds are present.
Keep the original installer assets for first-time installs and recovery.

Before the first rollout, verify two consecutive versions on a test machine:
install A, accept the update to B, confirm the app reopens with its existing
database and settings, and confirm it does not offer B again. Exercise both
Windows installers and both Mac architectures. If GitHub is unavailable,
startup should still work; a failed download or invalid signature must not
replace the app.
