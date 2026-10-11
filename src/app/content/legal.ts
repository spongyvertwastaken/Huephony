import gplLicense from "../../../LICENSE?raw";

export const privacyPolicy = `# Privacy

Effective October 11, 2026

Huephony is a Windows app that reads your current Spotify track and matches your screen and Govee lights to its album artwork.

## What stays private

Huephony has no servers of its own. The developer does not receive your credentials, listening information, audio, or diagnostic logs. There are no ads, analytics, tracking, or crash reporting.

## Spotify

When you connect, Spotify grants permission to read your current track and playback state and to control playback. Huephony reads the track's title, artists, album, artwork, duration and position, whether it is playing, the active device's volume, and the next queued track so its colors can be prepared.

Playback controls are sent when you use the app's play, pause, skip, seek, or volume controls. The app does not request your email, profile, playlists, saved music, listening history, followers, or payment information.

The app connects to Spotify's sign-in and API services and its artwork servers. Spotify handles these connections under its own [Privacy Policy](https://www.spotify.com/legal/privacy-policy/).

## Pulse and computer audio

Pulse reads sound playing through your computer's audio output and immediately reduces it to a loudness level. It never uses a microphone. No audio is recorded, stored, or uploaded; loudness levels are held only in memory.

Where possible, it follows Spotify's output device and checks Spotify's Windows volume meter. This is output-device audio, so other sounds on the same device can affect the level while Spotify is playing. Without a detectable Spotify desktop session, all audio on the selected output can count.

Audio capture runs while Pulse needs a level, including its Settings preview, and stops shortly after the last request. Other light effects do not need audio capture.

## Govee lights

Huephony searches for and controls your lights directly on your home network. It sends power, color, brightness, and status commands using Govee's LAN protocol. It does not use your Govee account or Govee's cloud API.

## Files on your PC

Settings, developer credentials, Spotify sign-in tokens, and diagnostic logs are stored in **%APPDATA%\\SpotifyRoomLighting**. The folder name is retained for compatibility with earlier versions.

Settings include your light names, models, device IDs, IP addresses, room layout, and display preferences. Logs can include track names, light addresses, and errors. The Python log rotates; the desktop sidecar log is saved separately.

Windows Data Protection (DPAPI) protects the Spotify secret and sign-in tokens when available. The current implementation can fall back to plain storage if encryption is unavailable or fails.

The app's WebView also keeps a normal artwork/browser cache in **%LOCALAPPDATA%\\com.local.spotify-room-lighting**. Current playback and analyzed palettes are kept in memory while the app runs.

The local backend listens on this computer's loopback interface by default.

## Disconnecting and deleting data

Use **Settings → Spotify → Disconnect** to remove the saved sign-in, current listening state, in-memory palette cache, and diagnostic logs. The app stops accessing your Spotify account until you connect again. App preferences and your developer-app credentials remain.

You can also revoke access at [spotify.com/account/apps](https://www.spotify.com/account/apps/).

To remove all local app data, uninstall Huephony and delete the two folders above. Disconnect does not clear the WebView's browser cache.

## Your developer app

The current setup uses a developer app that you create in Spotify's dashboard. Spotify may record its API usage under Spotify's terms. Huephony's developer cannot access that account or developer app.

Huephony is not directed at children. You must meet Spotify's account age requirements. Updates to this policy ship with the app and its source.
`;

export const termsOfUse = `# License and use

Huephony is free software, copyright © 2026 Huephony contributors, distributed under the **GNU General Public License, version 3**. You may use, modify, and redistribute it under that license. When distributing a covered modified version, provide its corresponding source and preserve the required notices and license.

It is provided without warranty. The full license below describes your rights and the limits of liability. Third-party components keep their own licenses, listed under Open-source notices.

## Spotify and Govee

Huephony is an independent project. It is not affiliated with, sponsored by, or endorsed by Spotify or Govee. Their names and marks belong to their owners.

You are responsible for your Spotify account and developer app and for following Spotify's [Terms of Use](https://www.spotify.com/legal/end-user-agreement/), [Developer Terms](https://developer.spotify.com/terms), and [Developer Policy](https://developer.spotify.com/policy). The software license does not grant rights to third-party music, artwork, trademarks, or services.

Spotify and Govee may change or restrict their services, and those changes may affect Huephony. The current Spotify developer-app setup requires Spotify Premium.

## Changing lights

Huephony changes light color and brightness automatically. The Blink control flashes a bulb. If flashing or changing lights make you uncomfortable, stop using the effects. Do not rely on these lights for safety lighting. Install and operate devices as their manufacturer instructs.

## GNU General Public License

` + gplLicense;
