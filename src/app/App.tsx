import { useState, useEffect, useRef, type CSSProperties } from "react";
import { AnimatePresence } from "motion/react";
import { ImmersivePlayer } from "../features/player/ImmersivePlayer";
import type { SceneColor } from "../features/player/LiquidScene";
import { glassStyle } from "../features/player/glass";
import { SettingsPanel, type SaveState } from "../features/settings/SettingsPanel";
import { isTauri } from "@tauri-apps/api/core";
import { open as openExternal } from "@tauri-apps/plugin-shell";
import {
  applyLightPalette,
  disconnectSpotify,
  fetchNowPlaying,
  fetchSettings,
  nextSpotifyTrack,
  pauseSpotify,
  playSpotify,
  previousSpotifyTrack,
  saveSettings,
  seekSpotify,
  setSpotifyVolume,
  spotifyLoginUrl,
  testLights,
  turnLightsOff,
} from "../lib/api";
import type { AppSettings, LightCommandResult, NowPlayingState, PlayerCommandResult } from "../lib/types";
import { artworkSize } from "../features/player/artwork";
import { DesktopWindowControls } from "../components/DesktopWindowControls";

interface DisplayTrack {
  id: string;
  title: string;
  artist: string;
  album: string;
  coverUrl: string;
  url?: string;
}

const FALLBACK_PALETTE = ["#5fc6bc", "#ed866f", "#697eae", "#e8b590", "#486867", "#0a141a"];

// /api/now-playing only returns the backend's latest playback snapshot; it
// never calls Spotify. Polling it every second costs nothing and shows a new
// song within about a second of the backend seeing it.
const NOW_PLAYING_ACTIVE_POLL_MS = 1000;
const NOW_PLAYING_IDLE_POLL_MS = 2500;

function nextNowPlayingPollDelay(state: NowPlayingState | null) {
  return state?.authenticated && state.track ? NOW_PLAYING_ACTIVE_POLL_MS : NOW_PLAYING_IDLE_POLL_MS;
}

// Settings edits save on their own after this pause, so sliders and typing
// settle into one write.
const SETTINGS_SAVE_DELAY_MS = 450;

export default function App() {
  const [nowPlaying, setNowPlaying] = useState<NowPlayingState | null>(null);
  const [loading, setLoading] = useState(true);
  const [backendError, setBackendError] = useState("");
  const [lightsOn, setLightsOn] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [settingsLoadMessage, setSettingsLoadMessage] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const saveTimerRef = useRef<ReturnType<typeof setTimeout>>();
  // Edits not yet sent, and edits sent but not yet confirmed. A settings
  // reload lays both over what the backend returns, so it never shows an
  // older value.
  const pendingSaveRef = useRef<Partial<AppSettings>>({});
  const sendingSaveRef = useRef<Partial<AppSettings>>({});
  // Settings writes run one at a time so an older save never lands last.
  const saveChainRef = useRef<Promise<unknown>>(Promise.resolve());
  const [playerBusy, setPlayerBusy] = useState(false);
  const [playerMessage, setPlayerMessage] = useState("");
  const [refreshNowPlaying, setRefreshNowPlaying] = useState(0);
  const [volume, setVolume] = useState(50);
  const lastAudibleVolumeRef = useRef(50);
  const [displayProgressMs, setDisplayProgressMs] = useState(0);
  const progressSyncRef = useRef({ baseMs: 0, atTimestamp: 0, isPlaying: false, durationMs: 0 });
  // The Spotify read the progress bar is anchored to.
  const progressReadRef = useRef("");
  const currentTrackIdRef = useRef<string | null>(null);
  const nowPlayingFingerprintRef = useRef("");

  const palette =
    nowPlaying?.palette?.length && nowPlaying.palette.length >= 3
      ? nowPlaying.palette
      : FALLBACK_PALETTE;
  const [c1, c2, c3, c4] = [...palette, ...FALLBACK_PALETTE];
  // The background flows the artwork's real colors at their real coverage
  // (LiquidScene evens out the extremes); idle screens use the palette.
  const swatches = nowPlaying?.track ? (nowPlaying.smart_color_boost?.swatches ?? []) : [];
  const sceneColors: SceneColor[] = swatches.length
    ? swatches.map(({ hex, share }) => ({ hex, weight: share }))
    : palette.map((hex) => ({ hex, weight: 1 }));
  const rootStyle = {
    minHeight: "100vh",
    "--cover-c1": c1,
    "--cover-c2": c2,
    "--cover-c3": c3,
    "--cover-c4": c4,
  } as CSSProperties & Record<string, string | number>;
  const spotifyTrack = nowPlaying?.track;
  const track: DisplayTrack = spotifyTrack
    ? {
        id: spotifyTrack.id,
        title: spotifyTrack.title,
        artist: spotifyTrack.artist,
        album: spotifyTrack.album,
        coverUrl: spotifyTrack.cover_url,
        url: spotifyTrack.external_url,
      }
    : {
        id: nowPlaying?.mode ?? "idle",
        title: "",
        artist: "",
        album: "",
        coverUrl: "",
      };
  const isPlaying = Boolean(nowPlaying?.is_playing && spotifyTrack);
  const upNextCover = nowPlaying?.up_next_cover_url;
  const playerMessageTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const volumeTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const optimisticPlaybackRef = useRef<{ isPlaying: boolean; until: number } | null>(null);
  useEffect(() => {
    let mounted = true;
    let abortController: AbortController | null = null;
    let inFlight = false;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;

    const scheduleNextPoll = (state: NowPlayingState | null) => {
      clearTimeout(pollTimer);
      if (!mounted || document.hidden) return;
      pollTimer = setTimeout(loadNowPlaying, nextNowPlayingPollDelay(state));
    };

    const loadNowPlaying = async () => {
      if (inFlight) return;
      inFlight = true;
      abortController = new AbortController();
      let nextState: NowPlayingState | null = null;
      try {
        const next = await fetchNowPlaying(abortController.signal);
        if (!mounted) return;
        nextState = next;
        const fingerprint = JSON.stringify(next);
        const optimisticPlayback = optimisticPlaybackRef.current;
        let overridden = false;
        let expired = false;
        if (
          optimisticPlayback &&
          Date.now() < optimisticPlayback.until &&
          next.track?.id === currentTrackIdRef.current
        ) {
          next.is_playing = optimisticPlayback.isPlaying;
          next.mode = optimisticPlayback.isPlaying ? "Cover Sync" : "Paused";
          next.message = optimisticPlayback.isPlaying
            ? "Playing from Spotify."
            : "Spotify playback is paused.";
          overridden = true;
        } else if (optimisticPlayback && Date.now() >= optimisticPlayback.until) {
          optimisticPlaybackRef.current = null;
          expired = true;
        }
        // This runs every second; an unchanged snapshot skips the re-render.
        if (overridden || expired || fingerprint !== nowPlayingFingerprintRef.current) {
          nowPlayingFingerprintRef.current = overridden ? "" : fingerprint;
          setNowPlaying(next);
        }
        setBackendError("");
      } catch (error) {
        if (!mounted) return;
        const message = error instanceof Error ? error.message : "Could not reach backend";
        setBackendError(message);
      } finally {
        inFlight = false;
        if (mounted) {
          setLoading(false);
          scheduleNextPoll(nextState);
        }
      }
    };

    const loadWhenVisible = () => {
      if (!document.hidden) {
        clearTimeout(pollTimer);
        loadNowPlaying();
      }
    };

    loadNowPlaying();
    window.addEventListener("focus", loadNowPlaying);
    document.addEventListener("visibilitychange", loadWhenVisible);

    return () => {
      mounted = false;
      abortController?.abort();
      clearTimeout(pollTimer);
      window.removeEventListener("focus", loadNowPlaying);
      document.removeEventListener("visibilitychange", loadWhenVisible);
    };
  }, [refreshNowPlaying]);

  useEffect(() => {
    const abortController = new AbortController();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;

    const loadSettings = () => {
      attempts += 1;
      fetchSettings(abortController.signal)
        .then((next) => {
          setSettings(withUnsaved(next));
          setLightsOn(Boolean(next.lights_enabled));
        })
        .catch(() => {
          if (abortController.signal.aborted || attempts >= 8) return;
          retryTimer = setTimeout(loadSettings, 800);
        });
    };

    loadSettings();

    return () => {
      abortController.abort();
      clearTimeout(retryTimer);
    };
  }, []);

  useEffect(() => {
    currentTrackIdRef.current = track.id;
  }, [track.id]);

  useEffect(() => {
    if (typeof spotifyTrack?.volume_percent === "number") {
      setVolume(spotifyTrack.volume_percent);
      if (spotifyTrack.volume_percent > 0) {
        lastAudibleVolumeRef.current = spotifyTrack.volume_percent;
      }
    }
  }, [spotifyTrack?.volume_percent]);

  // Re-anchor the progress bar to each new Spotify read, counting the time
  // since that read so the bar doesn't jump back.
  useEffect(() => {
    const progress = spotifyTrack?.progress_ms ?? 0;
    const duration = spotifyTrack?.duration_ms ?? 0;
    const readAt = spotifyTrack?.progress_at_ms ?? 0;
    const readKey = `${spotifyTrack?.id ?? ""}:${readAt}:${progress}`;
    const sync = progressSyncRef.current;
    const now = Date.now();
    // Same read, only play/pause flipped (an optimistic update): continue from
    // where the bar is now.
    let base =
      readKey === progressReadRef.current
        ? sync.baseMs + (sync.isPlaying ? now - sync.atTimestamp : 0)
        : progress + (isPlaying && readAt ? Math.max(0, now - readAt) : 0);
    if (duration) base = Math.min(duration, base);
    progressReadRef.current = readKey;
    progressSyncRef.current = { baseMs: base, atTimestamp: now, isPlaying, durationMs: duration };
    setDisplayProgressMs(base);
  }, [spotifyTrack?.id, spotifyTrack?.progress_ms, spotifyTrack?.progress_at_ms, spotifyTrack?.duration_ms, isPlaying]);

  // Advance the bar smoothly between polls while playing.
  useEffect(() => {
    if (!spotifyTrack?.duration_ms) return;
    const interval = setInterval(() => {
      const sync = progressSyncRef.current;
      const elapsed = sync.isPlaying ? Date.now() - sync.atTimestamp : 0;
      setDisplayProgressMs(Math.min(sync.durationMs, sync.baseMs + elapsed));
    }, 500);
    return () => clearInterval(interval);
  }, [spotifyTrack?.id, spotifyTrack?.duration_ms]);

  // Load the next queued cover ahead of time, so the switch shows it at once.
  useEffect(() => {
    if (!upNextCover) return;
    const image = new Image();
    image.crossOrigin = "anonymous";
    image.src = upNextCover;
  }, [upNextCover]);

  useEffect(() => {
    if (!settingsOpen) return;

    const abortController = new AbortController();
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;

    const loadSettings = () => {
      attempts += 1;
      fetchSettings(abortController.signal)
        .then((next) => {
          setSettings(withUnsaved(next));
          setLightsOn(Boolean(next.lights_enabled));
          setSettingsLoadMessage("");
        })
        .catch((error) => {
          if (abortController.signal.aborted) return;
          if (attempts < 6) {
            setSettingsLoadMessage("Starting the background service...");
            retryTimer = setTimeout(loadSettings, 700);
            return;
          }
          const message = error instanceof Error ? error.message : "Could not load settings";
          setSettingsLoadMessage(message);
        });
    };

    loadSettings();

    return () => {
      abortController.abort();
      clearTimeout(retryTimer);
    };
  }, [settingsOpen]);

  useEffect(() => {
    return () => {
      clearTimeout(playerMessageTimerRef.current);
      clearTimeout(volumeTimerRef.current);
      clearTimeout(saveTimerRef.current);
    };
  }, []);

  function withUnsaved(next: AppSettings): AppSettings {
    return { ...next, ...sendingSaveRef.current, ...pendingSaveRef.current };
  }

  const queueSettingsSave = (payload: Partial<AppSettings>) => {
    const save = saveChainRef.current.catch(() => undefined).then(() => saveSettings(payload));
    saveChainRef.current = save;
    return save;
  };

  // Sends every pending edit now. Never rejects; a failure keeps the edits
  // pending and shows "Couldn't save" with a retry.
  const flushSettingsSave = (): Promise<void> => {
    clearTimeout(saveTimerRef.current);
    const payload = pendingSaveRef.current;
    if (!Object.keys(payload).length) return saveChainRef.current.then(() => undefined, () => undefined);
    pendingSaveRef.current = {};
    sendingSaveRef.current = { ...sendingSaveRef.current, ...payload };
    setSaveState("saving");
    const settle = (confirmed: boolean) => {
      const sending = { ...sendingSaveRef.current };
      for (const key of Object.keys(payload) as (keyof AppSettings)[]) {
        if (sending[key] === payload[key]) delete sending[key];
      }
      sendingSaveRef.current = sending;
      if (!confirmed) pendingSaveRef.current = { ...payload, ...pendingSaveRef.current };
    };
    return queueSettingsSave(payload).then(
      () => {
        settle(true);
        if (!Object.keys(pendingSaveRef.current).length && !Object.keys(sendingSaveRef.current).length) {
          setSaveState("saved");
        }
        // Re-read the snapshot so the room preview shows re-planned lights.
        setRefreshNowPlaying((value) => value + 1);
      },
      () => {
        settle(false);
        setSaveState("error");
      },
    );
  };

  const handleSettingsChange = (patch: Partial<AppSettings>) => {
    setSettings((prev) => (prev ? { ...prev, ...patch } : prev));
    pendingSaveRef.current = { ...pendingSaveRef.current, ...patch };
    setSaveState("saving");
    clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(flushSettingsSave, SETTINGS_SAVE_DELAY_MS);
  };

  const closeSettings = () => {
    flushSettingsSave();
    setSettingsOpen(false);
  };

  const handleLightsToggle = async () => {
    const nextLightsOn = !lightsOn;
    setLightsOn(nextLightsOn);
    setSettings((prev) => (prev ? { ...prev, lights_enabled: nextLightsOn } : prev));
    try {
      await queueSettingsSave({ lights_enabled: nextLightsOn });
      // The button's lit state is the feedback; only problems get a message.
      const result = nextLightsOn ? await applyLightPalette(palette) : await turnLightsOff();
      if (!result.ok || !result.connected) showPlayerMessage(result.message);
      if (nextLightsOn) setRefreshNowPlaying((value) => value + 1);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not update lights.";
      showPlayerMessage(message);
    }
  };

  const handleTestLights = async (): Promise<LightCommandResult> => {
    await flushSettingsSave();
    try {
      return await testLights();
    } catch (error) {
      return {
        ok: false,
        enabled: lightsOn,
        connected: false,
        message: error instanceof Error ? error.message : "Could not test lights.",
        devices: [],
      };
    }
  };

  // Credentials save on their own, not with the automatic saves: changing
  // them signs out of Spotify, so it only happens on "Save and connect".
  const handleSaveSpotify = async (patch: Partial<AppSettings>) => {
    await flushSettingsSave();
    const next = await queueSettingsSave(patch);
    setSettings(withUnsaved(next));
    setRefreshNowPlaying((value) => value + 1);
  };

  const handleDisconnectSpotify = async (): Promise<PlayerCommandResult> => {
    const result = await disconnectSpotify();
    setRefreshNowPlaying((value) => value + 1);
    return result;
  };

  const openLink = (url: string) => {
    if (!isTauri()) {
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    openExternal(url).catch(() => showPlayerMessage("Could not open the link in your browser."));
  };

  const handleLogin = async () => {
    const desktopLogin = isTauri();
    const loginUrl = spotifyLoginUrl(desktopLogin);
    if (!desktopLogin) {
      window.location.href = loginUrl;
      return;
    }

    showPlayerMessage("Opening Spotify login in your browser.");
    try {
      await openExternal(loginUrl);
      window.setTimeout(() => setRefreshNowPlaying((value) => value + 1), 1200);
      window.setTimeout(() => setRefreshNowPlaying((value) => value + 1), 3200);
    } catch {
      window.location.href = loginUrl;
    }
  };

  const showPlayerMessage = (message: string) => {
    setPlayerMessage(message);
    clearTimeout(playerMessageTimerRef.current);
    playerMessageTimerRef.current = setTimeout(() => setPlayerMessage(""), 3600);
  };

  const burstRefreshNowPlaying = (delays: number[]) => {
    delays.forEach((delay) => {
      window.setTimeout(() => setRefreshNowPlaying((value) => value + 1), delay);
    });
  };

  const runPlayerCommand = async (
    command: () => Promise<PlayerCommandResult>,
    optimisticPlaying?: boolean,
  ) => {
    if (playerBusy) return;
    setPlayerBusy(true);
    try {
      const result = await command();
      if (!result.ok) showPlayerMessage(result.message);
      if (result.ok && typeof optimisticPlaying === "boolean") {
        optimisticPlaybackRef.current = {
          isPlaying: optimisticPlaying,
          until: Date.now() + 2600,
        };
        nowPlayingFingerprintRef.current = "";
        setNowPlaying((prev) =>
          prev
            ? {
                ...prev,
                is_playing: optimisticPlaying,
                mode: optimisticPlaying ? "Cover Sync" : "Paused",
                message: optimisticPlaying ? "Playing from Spotify." : "Spotify playback is paused.",
              }
            : prev,
        );
      }
      if (result.ok) {
        if (typeof optimisticPlaying === "boolean") {
          burstRefreshNowPlaying([900, 2600]);
        } else {
          burstRefreshNowPlaying([1200, 3600]);
        }
      }
    } finally {
      setPlayerBusy(false);
    }
  };

  const handleVolumeChange = (nextVolume: number) => {
    setVolume(nextVolume);
    if (nextVolume > 0) {
      lastAudibleVolumeRef.current = nextVolume;
    }
    clearTimeout(volumeTimerRef.current);
    volumeTimerRef.current = setTimeout(async () => {
      const result = await setSpotifyVolume(nextVolume).catch((error) => ({ ok: false, message: error instanceof Error ? error.message : "Could not change volume." }));
      if (!result.ok) {
        showPlayerMessage(result.message);
      }
    }, 260);
  };

  const handleToggleMute = async () => {
    clearTimeout(volumeTimerRef.current);
    const nextVolume = volume === 0 ? lastAudibleVolumeRef.current || 50 : 0;
    if (volume > 0) {
      lastAudibleVolumeRef.current = volume;
    }
    setVolume(nextVolume);
    const result = await setSpotifyVolume(nextVolume);
    if (!result.ok) {
      showPlayerMessage(result.message);
    }
  };

  const handleSeek = async (fraction: number) => {
    const duration = spotifyTrack?.duration_ms ?? 0;
    if (!duration) return;
    const target = Math.max(0, Math.min(duration, Math.round(fraction * duration)));
    progressSyncRef.current = {
      baseMs: target,
      atTimestamp: Date.now(),
      isPlaying,
      durationMs: duration,
    };
    setDisplayProgressMs(target);
    const result = await seekSpotify(target);
    if (!result.ok) {
      showPlayerMessage(result.message);
    } else {
      burstRefreshNowPlaying([700, 1800]);
    }
  };

  const safely = (action: () => void | Promise<unknown>) => {
    Promise.resolve().then(action).catch((error) => {
      showPlayerMessage(error instanceof Error ? error.message : "Could not reach Spotify. Try again.");
    });
  };

  return (
    <div className="size-full relative overflow-hidden" style={rootStyle}>
      <DesktopWindowControls />
      <ImmersivePlayer
        track={track}
        sceneColors={sceneColors}
        glass={glassStyle(settings?.glass_style)}
        artworkSize={artworkSize(settings?.artwork_size)}
        playing={isPlaying}
        authenticated={Boolean(nowPlaying?.authenticated)}
        loading={loading}
        offline={Boolean(backendError)}
        busy={playerBusy}
        lightsOn={lightsOn}
        progress={displayProgressMs}
        duration={spotifyTrack?.duration_ms ?? 0}
        volume={volume}
        message={playerMessage}
        onSettings={() => setSettingsOpen(true)}
        onLights={() => safely(handleLightsToggle)}
        onLogin={() => safely(handleLogin)}
        onPlay={() => safely(() => runPlayerCommand(isPlaying ? pauseSpotify : playSpotify, !isPlaying))}
        onPrevious={() => safely(() => runPlayerCommand(previousSpotifyTrack))}
        onNext={() => safely(() => runPlayerCommand(nextSpotifyTrack))}
        onSeek={(fraction) => safely(() => handleSeek(fraction))}
        onVolume={handleVolumeChange}
        onMute={() => safely(handleToggleMute)}
        onOpenLink={openLink}
      />

      <AnimatePresence>
        {settingsOpen && (
          <SettingsPanel
            settings={settings}
            loadMessage={settingsLoadMessage}
            nowPlaying={nowPlaying}
            accent={[c1, c2]}
            lightsOn={lightsOn}
            saveState={saveState}
            onClose={closeSettings}
            onChange={handleSettingsChange}
            onRetrySave={() => {
              flushSettingsSave();
            }}
            onToggleLights={() => safely(handleLightsToggle)}
            onTestLights={handleTestLights}
            onSaveSpotify={handleSaveSpotify}
            onConnectSpotify={() => safely(handleLogin)}
            onDisconnectSpotify={handleDisconnectSpotify}
            onOpenLink={openLink}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
