import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { fetchPulse } from "../../lib/api";
import { type AppSettings, type NowPlayingState, type PulseReading } from "../../lib/types";
import { roomSizes, slotLabel } from "../lights/layout";
import { Notice, Spinner, Card, Stepper } from "./ui";
import { LIGHT_EFFECTS, lightEffect, progressFrame, waveAxis, waveDirections, waveFrame, type AnimationSpeed, type Bulb, type LightEffect, type ProgressStyle, type WaveDirection } from "../lights/effects";

const EFFECT_LABELS: Record<LightEffect, { name: string; sub: string }> = {
  still: { name: "Still", sub: "Each bulb keeps its color all song" },
  wave: { name: "Wave", sub: "Colors sweep across the room" },
  pulse: { name: "Pulse", sub: "Brightness follows the music" },
  progress: { name: "Progress", sub: "The room fills as the song plays" },
};

const SPEED_OPTIONS: { value: AnimationSpeed; label: string }[] = [
  { value: "slow", label: "Slow" },
  { value: "medium", label: "Medium" },
  { value: "fast", label: "Fast" },
];

const DIRECTION_OPTIONS: { value: WaveDirection; label: string }[] = [
  { value: "across", label: "Left to right" },
  { value: "down", label: "Top to bottom" },
];

const PROGRESS_OPTIONS: { value: ProgressStyle; label: string; sub: string }[] = [
  { value: "fill", label: "Fill", sub: "Stands light up one by one as the song plays." },
  { value: "blend", label: "Blend", sub: "The room fades from the song's main color to its accent." },
];

// Without a song, the room preview still shows the chosen effect.
const DEMO_PROGRESS_SECONDS = 10;

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div className="st-segmented" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

// Re-renders every intervalMs while active; returns the time of the render.
function useTicker(active: boolean, intervalMs: number) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(performance.now()), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);
  return now;
}

// Polls the Pulse effect's live loudness while active.
function usePulse(active: boolean) {
  const [reading, setReading] = useState<PulseReading | null>(null);
  useEffect(() => {
    if (!active) {
      setReading(null);
      return;
    }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      let delay = 150;
      try {
        setReading(await fetchPulse(controller.signal));
      } catch {
        if (controller.signal.aborted) return;
        delay = 1500;
      }
      timer = setTimeout(poll, delay);
    };
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [active]);
  return reading;
}

function songProgress(nowPlaying: NowPlayingState | null): number | null {
  const track = nowPlaying?.track;
  if (!track?.duration_ms) return null;
  const since = nowPlaying?.is_playing && track.progress_at_ms ? Date.now() - track.progress_at_ms : 0;
  return Math.max(0, Math.min(1, (track.progress_ms + since) / track.duration_ms));
}

function PulseStatus({ reading }: { reading: PulseReading | null }) {
  if (!reading) {
    return (
      <span className="st-pulse-status">
        <Spinner size={12} />
        Listening...
      </span>
    );
  }
  if (!reading.available) {
    return (
      <Notice tone="warning" title="Pulse can't hear this computer">
        {reading.message} Bulbs keep a steady brightness instead.
      </Notice>
    );
  }
  if (!reading.hearing) {
    return (
      <Notice tone="info" title="No music playing on this computer">
        Pulse follows Spotify playing through this computer's speakers or headphones. Until then, bulbs keep a steady
        brightness.
      </Notice>
    );
  }
  return (
    <div className="st-pulse-status">
      <span className="st-pulse-meter" aria-hidden="true">
        <span style={{ width: `${Math.round(reading.level * 100)}%` }} />
      </span>
      {reading.source === "spotify" ? "Hearing Spotify on this computer." : "Hearing everything this computer plays."}
    </div>
  );
}

function EffectCard({
  settings,
  sizes,
  pulse,
  onChange,
}: {
  settings: AppSettings;
  sizes: number[];
  pulse: PulseReading | null;
  onChange: (patch: Partial<AppSettings>) => void;
}) {
  const effect = lightEffect(settings.light_effect);
  const directions = waveDirections(sizes);
  const style = PROGRESS_OPTIONS.find((option) => option.value === settings.progress_style) ?? PROGRESS_OPTIONS[0];
  return (
    <Card title="Light effect" sub="How the light moves around the room while a song plays. Paused, it holds still.">
      <div className="st-effect-options" role="radiogroup" aria-label="Light effect">
        {LIGHT_EFFECTS.map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={effect === value}
            className="st-glass st-effect"
            onClick={() => onChange({ light_effect: value })}
          >
            <span className={`st-effect-art st-effect-art--${value}`} aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
            <span className="st-glass-name">{EFFECT_LABELS[value].name}</span>
            <span className="st-glass-sub">{EFFECT_LABELS[value].sub}</span>
          </button>
        ))}
      </div>

      {effect === "wave" ? (
        <div className="st-grid-2">
          {directions.length > 1 ? (
            <div className="st-control">
              <span className="st-row-title">Direction</span>
              <Segmented
                label="Wave direction"
                value={waveAxis(settings.wave_direction, sizes)}
                options={DIRECTION_OPTIONS}
                onChange={(value) => onChange({ wave_direction: value })}
              />
            </div>
          ) : null}
          <div className="st-control">
            <span className="st-row-title">Speed</span>
            <Segmented
              label="Wave speed"
              value={SPEED_OPTIONS.some((option) => option.value === settings.animation_speed) ? settings.animation_speed : "slow"}
              options={SPEED_OPTIONS}
              onChange={(value) => onChange({ animation_speed: value })}
            />
          </div>
        </div>
      ) : null}

      {effect === "pulse" ? (
        <div className="st-control">
          <PulseStatus reading={pulse} />
          <span className="st-row-sub">
            Choruses go up to full brightness and quiet parts dim right down. Nothing is recorded: the app only
            measures how loud the sound is, on this computer.
          </span>
        </div>
      ) : null}

      {effect === "progress" ? (
        <div className="st-control">
          <Segmented
            label="Progress style"
            value={style.value}
            options={PROGRESS_OPTIONS}
            onChange={(value) => onChange({ progress_style: value })}
          />
          <span className="st-row-sub">{style.sub} It starts over with each new song.</span>
        </div>
      ) : null}
    </Card>
  );
}

export function RoomSection({
  settings,
  plan,
  nowPlaying,
  accent,
  onChange,
  onOpenLights,
}: {
  settings: AppSettings;
  plan: string[];
  nowPlaying: NowPlayingState | null;
  accent: [string, string];
  onChange: (patch: Partial<AppSettings>) => void;
  onOpenLights: () => void;
}) {
  const sizes = roomSizes(settings);
  const count = sizes.reduce((sum, size) => sum + size, 0);
  const starts = sizes.map((_, stand) => sizes.slice(0, stand).reduce((sum, size) => sum + size, 0));
  const places = sizes.flatMap((size, stand) => Array.from({ length: size }, (_, position): [number, number] => [stand, position]));
  const planReady = plan.length === count;
  const effect = lightEffect(settings.light_effect);
  const sample = !planReady && effect !== "still";
  const shown = planReady ? plan : sample ? places.map((_, slot) => accent[slot % 2]) : [];
  const direction = waveAxis(settings.wave_direction, sizes);
  const speed = settings.animation_speed ?? "slow";
  const pulse = usePulse(effect === "pulse");
  const now = useTicker(effect === "wave" || effect === "progress", effect === "wave" ? 100 : 400);
  const waveStart = useMemo(() => performance.now(), [effect, shown.join(), direction, speed]);
  const progress = songProgress(nowPlaying) ?? ((now / 1000) % DEMO_PROGRESS_SECONDS) / DEMO_PROGRESS_SECONDS;

  let frame: Bulb[] = shown.map((color) => [color, 1]);
  if (shown.length === count) {
    if (effect === "wave") frame = waveFrame(places, sizes, shown, (now - waveStart) / 1000, direction, speed);
    if (effect === "progress") frame = progressFrame(places, sizes, shown, progress, settings.progress_style);
    if (effect === "pulse" && pulse?.hearing) frame = shown.map((color) => [color, pulse.level]);
  }

  return (
    <div className="st-stack">
      <Card title="Layout" sub="Bulbs are spread evenly across your stands, so every stand gets contrasting colors.">
        <div className="st-grid-2">
          <Stepper
            label="Lights"
            value={count}
            min={1}
            max={32}
            onChange={(next) => onChange({ light_count: next, light_stands: Math.min(sizes.length, next) })}
          />
          <Stepper
            label="Stands"
            value={sizes.length}
            min={1}
            max={count}
            onChange={(next) => onChange({ light_count: count, light_stands: next })}
          />
        </div>
        <div
          className={`st-room${effect !== "still" ? " st-room--live" : ""}`}
          role="img"
          aria-label={planReady ? `Light colors for the current song, ${EFFECT_LABELS[effect].name} effect` : "Room layout"}
        >
          {sizes.map((size, stand) => (
            <div key={stand} className="st-stand">
              {Array.from({ length: size }, (_, position) => {
                const slot = starts[stand] + position;
                const [color, level] = frame[slot] ?? [];
                // Effects use the bulbs' full range, whatever the brightness setting.
                const lit = effect === "still" ? 1 : (level ?? 1);
                return (
                  <span
                    key={position}
                    title={slotLabel(sizes, slot)}
                    className={`st-bulb${color ? " st-bulb--lit" : ""}`}
                    style={
                      color
                        ? ({ background: color, "--glow": `${color}aa`, "--lit": lit.toFixed(3) } as CSSProperties)
                        : undefined
                    }
                  />
                );
              })}
              <span className="st-pole" />
              <span className="st-stand-label">Stand {stand + 1}</span>
            </div>
          ))}
        </div>
        <p className="st-hint">
          {planReady
            ? "These are the colors each spot gets for the song playing now. "
            : sample
              ? "Sample colors. Play a song on Spotify to see its own. "
              : "Play a song on Spotify to preview its colors here. "}
          {effect === "progress" && planReady && songProgress(nowPlaying) !== null
            ? `${Math.round(progress * 100)}% through the song. `
            : ""}
          Choose which bulb sits in each spot under{" "}
          <button type="button" className="st-link" onClick={onOpenLights}>
            Lights
          </button>
          .
        </p>
      </Card>

      <EffectCard settings={settings} sizes={sizes} pulse={pulse} onChange={onChange} />
    </div>
  );
}
