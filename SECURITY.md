# Security

## Reporting a vulnerability

Please don't open a public issue for security problems. Use GitHub's private reporting instead: go to the **Security** tab of this repository and choose **Report a vulnerability**.

Include what you found, how to reproduce it, and which version you tested. You can expect a first reply within a few days.

## Scope

Huephony runs locally and has no server of its own. Things worth reporting include:

- Leaking Spotify credentials, tokens or other secrets, including through logs
- The local backend (`127.0.0.1:8000`) accepting requests it shouldn't
- Problems in how the installer or the bundled backend are built or launched

## Supported versions

Only the latest release gets fixes.
