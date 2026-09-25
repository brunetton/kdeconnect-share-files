# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-09-25

### Added

- Refresh button in the window header bar, above the device list, to look for
  reachable devices again. It asks the daemon for a network discovery, so the
  devices that just came online show up without restarting the app. The list
  stays on screen while the daemon looks, a spinner replaces the button icon,
  and the device that was already selected stays selected.

## [1.0.0] - 2026-09-18

First release: send files to a device paired in KDE Connect from a single GTK4
window.

### Added

- Device discovery through the running `kdeconnectd` daemon over the session
  bus (`org.kde.kdeconnect`), instead of shelling out to `kdeconnect-cli`.
- Device list limited to the paired, reachable devices that accept files, with
  an icon and a label for the kind of device (phone, tablet, computer, TV).
- File picker when the command is run without argument, and files that can be
  dropped on the window at any time; local paths and URLs are both accepted.
- Explicit error pages, with a *Try again* button, when no session bus is
  available, when the daemon is not running, when no device can receive files,
  when the selected device went offline, or when a file does not exist.

### Fixed

- A single click on a device only selects it: sending the files now needs the
  *Send* button or a double click.
