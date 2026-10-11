# Huephony

I wanted my room lights to follow whatever I was listening to, so I built this. Huephony grabs the colors from the Spotify album cover that's playing, spreads them across your Govee lights, and shows a matching full-screen display on your PC.

It's free, Windows-only, and everything runs on your own machine. No accounts, no ads, no analytics.

## What you need

- Windows 10 or 11, 64-bit
- Govee Wi-Fi lights with **LAN Control** switched on (it's a toggle in each light's settings in Govee Home). Support varies by model. Each light is treated as one color, so strips won't do per-segment effects.
- Your PC and lights on the same home network. Guest networks and "client isolation" will stop discovery from working.
- Spotify Premium and your own Spotify developer app. Spotify requires Premium for the owner of a development-mode app, which is annoying, but that's the rule. See [their docs](https://developer.spotify.com/documentation/web-api/concepts/quota-modes).

Pulse mode also needs Spotify to be playing through this PC's speakers or headphones. The other modes just read what Spotify says is playing.

## Getting it running

Grab `Huephony_<version>_x64-setup.exe` from the [Releases page](https://github.com/spongyvertwastaken/huephony/releases) and run it. The installer is unsigned, so Windows SmartScreen will complain. You don't need Python or anything else installed. If there's no release yet, you can build it yourself (see below).

**Spotify**

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and create an app. Pick **Web API**.
2. Add this redirect URI exactly: `http://127.0.0.1:8000/callback`
3. Make sure the Spotify account you'll use is listed under the app's **Users Management**.
4. In Huephony, open **Settings → Spotify**, paste the Client ID and Client Secret, and hit **Save and connect**.
5. Approve the permissions in your browser, then come back to the app.

Huephony reads what's playing and what's queued next, and can control playback. It doesn't ask for your profile, playlists or listening history. Keep your Client Secret to yourself.

**Lights**

1. Set your lights up in Govee Home and turn on **LAN Control** for each one.
2. Open **Settings → Lights → Find my lights** and add what it finds. If Windows asks, let the app through the firewall on your private network.
3. Hit **Blink** on each one to see which bulb is which, and give it a name.
4. Under **Room**, tell it how many lights you have and how they're grouped into stands, then give each light a spot.
5. Turn on **Sync lights to music** and play something.

If discovery misses a light, add it by IP address. You never need a Govee cloud API key.

## Light effects

| Mode | What it does |
| --- | --- |
| **Still** | Each bulb holds a color picked from the cover. |
| **Wave** | Colors sweep across your stands or down them, at a slow, medium or fast pace. |
| **Pulse** | Colors stay put and brightness follows how loud the audio is. |
| **Progress** | **Fill** lights the room up one stand at a time as the song goes on. **Blend** slowly fades from the main color to an accent. |

The room preview in the app shows what your layout will look like. The color planner tries to give neighboring bulbs contrasting colors and keep the smaller accent colors visible, even on mostly white artwork.

Pulse listens to your PC's output audio locally. It doesn't use a microphone and doesn't record anything. It tracks loudness, not individual beats, and other sounds on the same output can nudge it while Spotify is playing. If it can't find a Spotify desktop session, it falls back to whatever the PC is playing.

The **Bulb brightness** range only applies to Still. The moving effects use their own range. By default the lights hold their last frame while playback is paused.

Commands to each bulb are throttled (about 0.35 seconds apart by default), so this is ambient lighting, not frame-perfect.

## The screen

There are eight glass styles: Frosted, Fluted, Rain, Glass block, Stained glass, Pool, Crystal and Bokeh, all tinted by the album cover. The player has the usual play/pause, skip, seek and volume controls. You can hide them for a cover-only view, resize the cover, or freeze the background motion. Settings save as you change them.

## Privacy

Nothing goes to me. Huephony talks to Spotify for sign-in, artwork and playback info, and sends light commands straight to your bulbs over your LAN.

What it keeps on your PC:

- Settings, developer credentials, sign-in tokens and logs in `%APPDATA%\SpotifyRoomLighting`
- The WebView's artwork cache in `%LOCALAPPDATA%\com.local.spotify-room-lighting`

Those folder names are left over from the app's old name, and I've kept them so upgrades don't lose your settings. Secrets are protected with Windows DPAPI when it's available, and can fall back to plain storage if encryption fails. Logs can include track names and device addresses.

**Disconnect Spotify** removes your sign-in, the current playback state, the in-memory palette cache and the logs, but keeps your preferences and developer credentials. To wipe everything, uninstall and delete the two folders above. The full privacy policy and third-party notices are in **Settings → Privacy & legal**.

## Troubleshooting

- **No lights found:** check that LAN Control is on, the light is powered and reachable, and the Windows firewall allows the app on private networks. If your router blocks multicast, add the light by IP. Another light-control app can also be hogging the discovery reply port.
- **Connected to Spotify but nothing shows:** start playing something, and check the account is allowed in your developer app. The app owner's Premium subscription has to stay active.
- **Pulse isn't moving:** play Spotify through this PC, check your output device, and look at the Pulse status in Settings.
- **Backend offline:** restart Huephony and make sure nothing else is using port 8000.
- **Wrong bulb changes color:** use Blink again and check which spot it's assigned to.

If you [open an issue](https://github.com/spongyvertwastaken/huephony/issues), include your Windows version, Govee model, the effect you were using and how to reproduce it. Scrub credentials, tokens and personal info out of logs and screenshots first.

## Building it yourself

You'll need Node.js 22+, Python 3.13, the Rust stable MSVC toolchain, and Microsoft's C++ build tools with the Windows SDK. Then, from the project folder in PowerShell:

```powershell
npm.cmd ci
npm.cmd run backend:venv
npm.cmd run backend:install
npm.cmd run desktop:build
```

The installer lands in `src-tauri/target/release/bundle/nsis/`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow, tests and project layout. Small, focused pull requests are welcome.

## License

GPL-3.0. See [LICENSE](LICENSE). If you distribute a modified version, you have to keep the license and share your source. Contributions come in under the same license. Bundled dependencies, fonts and shader snippets keep their own licenses, which are listed in the app's open-source notices (regenerate them with `npm run notices`).

Huephony isn't affiliated with Spotify or Govee, and their own terms still apply to their services.
