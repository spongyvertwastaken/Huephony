# Contributing to Huephony

Thanks for helping out. Small, focused pull requests are easiest to review.

## Before you start

- For anything bigger than a bug fix, open an issue first so we can agree on the approach.
- If you're adding support for a light, say which model you tested and which LAN commands it takes.
- For color or effect changes, include an example that reproduces the problem or shows the improvement, and update the tests.
- Contributions are licensed under GPL-3.0, the same as the project.

## Setup

You'll need Node.js 22+, Python 3.13, the Rust stable MSVC toolchain, and Microsoft's C++ build tools with the Windows SDK. From the project folder in PowerShell:

```powershell
npm.cmd ci
npm.cmd run backend:venv
npm.cmd run backend:install
```

For day-to-day work, run these in two terminals and open `http://127.0.0.1:5173`:

```powershell
npm.cmd run backend:dev
npm.cmd run dev
```

You can set Spotify up in Settings, or copy `.env.example` to `.env` (never commit the real one).

To try the native shell, stop the dev backend, then:

```powershell
npm.cmd run backend:build
npm.cmd run desktop:dev
```

The shell launches the compiled backend itself, so rebuild it after changing any Python.

## Checks

```powershell
npm.cmd test
```

This runs the TypeScript check and the backend tests. `npm run desktop:build` refreshes the third-party notices, builds everything and writes the installer to `src-tauri/target/release/bundle/nsis/`. GitHub Actions runs the same checks and build on Windows but doesn't publish releases.

## Project layout

```text
src/
  app/               entry point and the bundled legal text
  components/        shared bits (window controls, markdown)
  features/          player, lights (layout and effect previews), settings
  lib/               API client and shared types
  styles/
backend/
  main.py            the FastAPI app; wires up the routers
  routers/           HTTP routes: settings, Spotify sign-in, lights, player
  playback.py        the polling daemon that turns playback into light colors
  spotify_client.py  Spotify tokens, request budgets, player reads, controls
  settings.py        saved settings, defaults, migrations from older builds
  palette/           color analysis, artwork fetching, caching
  lights/            Govee transport, room planning, effects, audio
  tests/
src-tauri/           native window, backend lifecycle, installer
scripts/             third-party notice generation
docs/                example of the legacy lights.config.json that older builds migrate from
```

## Privacy rules for contributions

Huephony keeps everything on the user's machine. Please don't add analytics, telemetry or network calls to services other than Spotify and the user's own lights, and keep credentials and tokens out of logs, screenshots and test fixtures.
