import { useEffect, useState, type CSSProperties } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { AudioLines, Eye, EyeOff, Lightbulb, Pause, Play, Settings2, SkipBack, SkipForward, Volume2, VolumeX, Waves } from "lucide-react";
import { LiquidScene, type SceneColor } from "./LiquidScene";
import type { GlassStyle } from "./glass";
import "../../styles/immersive.css";

interface Props {
  track: { id: string; title: string; artist: string; album: string; coverUrl: string; url?: string };
  sceneColors: SceneColor[];
  glass: GlassStyle;
  // Cover size in artwork-only mode, as a percentage of the screen's short side.
  artworkSize: number;
  playing: boolean;
  authenticated: boolean;
  loading: boolean;
  offline: boolean;
  busy: boolean;
  lightsOn: boolean;
  progress: number;
  duration: number;
  volume: number;
  message: string;
  onSettings: () => void;
  onLights: () => void;
  onLogin: () => void;
  onPlay: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onSeek: (fraction: number) => void;
  onVolume: (volume: number) => void;
  onMute: () => void;
  onOpenLink: (url: string) => void;
}
const ARTWORK_ONLY_KEY = "listening-room:artwork-only";
const readArtworkOnly = () => { try { return localStorage.getItem(ARTWORK_ONLY_KEY) === "1"; } catch { return false; } };
const formatTime = (value: number) => `${Math.floor(Math.max(0, value) / 60000)}:${Math.floor(Math.max(0, value) / 1000 % 60).toString().padStart(2, "0")}`;

export function ImmersivePlayer(props: Props) {
  const { track, sceneColors, glass, playing, authenticated, loading, offline, busy, lightsOn, progress, duration, volume } = props;
  const [flowing, setFlowing] = useState(true);
  const [failedCover, setFailedCover] = useState("");
  const [seekDraft, setSeekDraft] = useState<number | null>(null);
  const [artworkOnly, setArtworkOnly] = useState(readArtworkOnly);
  const reduceMotion = useReducedMotion();
  const hasTrack = Boolean(track.title);
  const connected = authenticated && !offline;
  const disabled = !connected || !hasTrack || busy;
  const position = seekDraft ?? Math.min(progress, duration);
  const animationRunning = flowing && !reduceMotion;
  const hasCover = hasTrack && Boolean(track.coverUrl) && failedCover !== track.coverUrl;
  useEffect(() => { setSeekDraft(null); }, [track.id]);
  const toggleArtworkOnly = () => {
    const next = !artworkOnly;
    setArtworkOnly(next);
    try { localStorage.setItem(ARTWORK_ONLY_KEY, next ? "1" : "0"); } catch { /* per-viewer convenience only */ }
  };
  const commitSeek = (value: number) => {
    if (duration > 0) props.onSeek(value / duration);
    setSeekDraft(null);
  };

  return <main className={artworkOnly ? "listening-room listening-room--artwork-only" : "listening-room"} style={{ "--artwork-scale": props.artworkSize } as CSSProperties}>
    <LiquidScene colors={sceneColors} artworkUrl={hasCover ? track.coverUrl : ""} moving={animationRunning} glass={glass} />
    <div className="room-vignette" aria-hidden="true" />
    <div className="room-tools">
      <button className="icon-button" onClick={props.onLights} disabled={offline || loading} aria-label="Sync room lights" aria-pressed={lightsOn} title="Sync room lights"><Lightbulb size={17} /></button>
      <button className="icon-button" onClick={() => setFlowing(!flowing)} aria-label={animationRunning ? "Pause liquid motion" : "Resume liquid motion"} aria-pressed={animationRunning} disabled={Boolean(reduceMotion)} title={animationRunning ? "Pause motion" : "Resume motion"}><Waves size={17} /></button>
      <button className="icon-button" onClick={toggleArtworkOnly} aria-label={artworkOnly ? "Show player controls" : "Show artwork only"} aria-pressed={artworkOnly} title={artworkOnly ? "Show controls" : "Artwork only"}>{artworkOnly ? <Eye size={17} /> : <EyeOff size={17} />}</button>
      <button className="icon-button" onClick={props.onSettings} aria-label="Open settings" title="Settings"><Settings2 size={17} /></button>
    </div>
    <section className="player-composition" aria-label="Now playing">
      <div className="artwork-stage">
        <div className="album-cover"><AnimatePresence mode="sync"><motion.div className="album-image" key={track.id + track.coverUrl} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: reduceMotion ? 0 : 0.8 }}>
          {hasCover ? <img src={track.coverUrl} alt={`${track.album || track.title} artwork`} draggable={false} onError={() => setFailedCover(track.coverUrl)} /> : <div className="empty-artwork" role="img" aria-label={hasTrack ? "Artwork unavailable" : "Waiting for Spotify"}><AudioLines size={46} strokeWidth={.8} /></div>}
        </motion.div></AnimatePresence></div>
        <div className="artwork-volume">
          <input type="range" min="0" max="100" value={volume} disabled={disabled} onChange={(event) => props.onVolume(Number(event.target.value))} aria-label="Spotify volume" style={{ "--fill": `${volume}%` } as CSSProperties} />
          <button className="icon-button" onClick={props.onMute} disabled={disabled} aria-label={volume === 0 ? "Unmute Spotify" : "Mute Spotify"}>{volume === 0 ? <VolumeX size={17} /> : <Volume2 size={17} />}</button>
        </div>
      </div>
      <div className="player-details"><div className="player-details-inner">
      <div className="track-caption" aria-live="polite">
        {hasTrack ? <><h1>{track.url ? <a href={track.url} title="Open in Spotify" onClick={(event) => { event.preventDefault(); props.onOpenLink(track.url!); }}>{track.title}</a> : <span title={track.title}>{track.title}</span>}</h1><p title={track.artist}>{track.artist}</p></> :<div className="connection-state">{loading ? "Connecting…" : offline ? <button onClick={props.onSettings}>Spotify offline</button> : connected ? "Play a song on Spotify" : <button onClick={props.onLogin}>Connect Spotify</button>}</div>}
      </div>
      <div className="transport-buttons">
        <button className="icon-button" onClick={props.onPrevious} disabled={disabled} aria-label="Previous track" title="Previous"><SkipBack size={18} fill="currentColor" /></button>
        <button className="play-button" onClick={props.onPlay} disabled={disabled} aria-label={playing ? "Pause Spotify" : "Play Spotify"}>{playing ? <Pause size={20} fill="currentColor" /> : <Play size={20} fill="currentColor" className="play-icon" />}</button>
        <button className="icon-button" onClick={props.onNext} disabled={disabled} aria-label="Next track" title="Next"><SkipForward size={18} fill="currentColor" /></button>
      </div>
      <div className="timeline"><span>{formatTime(position)}</span><input type="range" min="0" max={duration || 1} step="1000" value={position} disabled={disabled || !duration} aria-label="Seek track position" aria-valuetext={`${formatTime(position)} of ${formatTime(duration)}`} onChange={(event) => setSeekDraft(Number(event.target.value))} onPointerUp={(event) => commitSeek(Number(event.currentTarget.value))} onPointerCancel={() => setSeekDraft(null)} onKeyUp={(event) => { if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) commitSeek(Number(event.currentTarget.value)); }} onBlur={(event) => { if (seekDraft !== null) commitSeek(Number(event.currentTarget.value)); }} style={{ "--fill": `${duration ? position / duration * 100 : 0}%` } as CSSProperties} /><span>{formatTime(duration)}</span></div>
      </div></div>
    </section>
    <AnimatePresence>{props.message && <motion.div role="status" className="room-toast" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} key={props.message}>{props.message}</motion.div>}</AnimatePresence>
  </main>;
}
