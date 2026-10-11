import { type CSSProperties, type ReactNode } from "react";
import { type AppSettings } from "../../lib/types";
import { ARTWORK_SIZE, artworkSize } from "../player/artwork";
import { GLASS_STYLES, glassStyle, type GlassStyle } from "../player/glass";
import { Card, Slider, RangeSlider, Disclosure } from "./ui";

const clampNumber = (value: unknown, min: number, max: number, fallback: number) => {
  const number = typeof value === "number" && Number.isFinite(value) ? value : fallback;
  return Math.min(max, Math.max(min, number));
};

// The backend's default update_speed_ms.
const RECOMMENDED_COMMAND_GAP_MS = 350;

const GLASS_LABELS: Record<GlassStyle, { name: string; sub: string }> = {
  frosted: { name: "Frosted", sub: "Soft, even blur" },
  fluted: { name: "Fluted", sub: "Ribbed, like reeded glass" },
  rain: { name: "Rain", sub: "Drops sliding down a window" },
  blocks: { name: "Glass block", sub: "Pillowed bricks, dark joints" },
  stained: { name: "Stained glass", sub: "Leaded panes, lit from behind" },
  pool: { name: "Pool", sub: "Ripples and dancing caustics" },
  crystal: { name: "Crystal", sub: "Cut facets that split the light" },
  bokeh: { name: "Bokeh", sub: "Out-of-focus lights drifting" },
};

// Swatch art is drawn on a 160×80 canvas. `seeded` gives the same scatter
// on every render.
const seeded = (index: number) => {
  const x = Math.sin(index * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};
const SWATCH_PANES = [
  "0,0 38,0 30,26 0,30",
  "38,0 92,0 64,34 30,26",
  "92,0 128,0 104,24 64,34",
  "128,0 160,0 160,28 140,32 104,24",
  "0,30 30,26 22,58 0,54",
  "30,26 64,34 58,52 22,58",
  "64,34 104,24 96,60 58,52",
  "104,24 140,32 126,50 96,60",
  "140,32 160,28 160,56 126,50",
  "0,54 22,58 44,80 0,80",
  "22,58 58,52 80,80 44,80",
  "58,52 96,60 118,80 80,80",
  "96,60 126,50 160,56 160,80 118,80",
];
const RAIN_BEADS = Array.from({ length: 30 }, (_, i) => ({
  x: seeded(i) * 160,
  y: seeded(i + 40) * 80,
  r: 1.2 + seeded(i + 80) ** 2 * 3.8,
}));
const teardrop = (x: number, y: number, r: number) =>
  `M${x} ${y - 2.3 * r}C${x + 0.4 * r} ${y - 1.3 * r} ${x + r} ${y - 0.7 * r} ${x + r} ${y}A${r} ${r} 0 0 1 ${x - r} ${y}C${x - r} ${y - 0.7 * r} ${x - 0.4 * r} ${y - 1.3 * r} ${x} ${y - 2.3 * r}Z`;
const BOKEH_DISCS = Array.from({ length: 12 }, (_, i) => ({
  x: seeded(i + 120) * 160,
  y: seeded(i + 160) * 80,
  r: 4 + seeded(i + 200) * 11,
  accent: i % 3 === 0 ? "var(--st-accent-2)" : "var(--st-accent)",
}));

const SWATCH_ART: Partial<Record<GlassStyle, ReactNode>> = {
  rain: (
    <svg viewBox="0 0 160 80" preserveAspectRatio="xMidYMid slice">
      <defs>
        <radialGradient id="st-swatch-drop" cx=".36" cy=".3" r=".8">
          <stop offset="0" stopColor="#fff" stopOpacity=".9" />
          <stop offset=".22" stopColor="#fff" stopOpacity=".22" />
          <stop offset=".7" stopColor="#fff" stopOpacity=".06" />
          <stop offset="1" stopColor="#000" stopOpacity=".65" />
        </radialGradient>
      </defs>
      <path d="M118 0V50M137 0V22" stroke="rgba(255,255,255,.1)" strokeWidth="5" strokeLinecap="round" />
      <g fill="url(#st-swatch-drop)">
        {RAIN_BEADS.map(({ x, y, r }, i) => (
          <circle key={i} cx={x} cy={y} r={r} />
        ))}
        <path d={teardrop(118, 54, 4.6)} />
        <path d={teardrop(137, 26, 3.8)} />
      </g>
    </svg>
  ),
  blocks: (
    <svg viewBox="0 0 160 80" preserveAspectRatio="xMidYMid slice">
      <defs>
        <linearGradient id="st-swatch-bevel" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#fff" stopOpacity=".28" />
          <stop offset=".3" stopColor="#fff" stopOpacity=".04" />
          <stop offset=".7" stopColor="#000" stopOpacity=".04" />
          <stop offset="1" stopColor="#000" stopOpacity=".32" />
        </linearGradient>
        <pattern id="st-swatch-block" x="10" y="10" width="20" height="20" patternUnits="userSpaceOnUse">
          <path d="M0 0H20V20H0ZM1.2 1.2V18.8H18.8V1.2Z" fillRule="evenodd" fill="rgba(8,8,12,.7)" />
          <rect x="1.2" y="1.2" width="17.6" height="17.6" rx="1.4" fill="url(#st-swatch-bevel)" />
          <rect x="4" y="4" width="12" height="12" rx="2" fill="none" stroke="rgba(255,255,255,.14)" strokeWidth=".6" />
        </pattern>
      </defs>
      <rect width="160" height="80" fill="url(#st-swatch-block)" />
    </svg>
  ),
  stained: (
    <svg viewBox="0 0 160 80" preserveAspectRatio="xMidYMid slice">
      {SWATCH_PANES.map((points, i) => (
        <polygon
          key={i}
          points={points}
          style={{
            fill: ["var(--st-accent)", "var(--st-accent-2)", "#fff", "#000"][i % 4],
            fillOpacity: [0.35, 0.4, 0.12, 0.3][(i * 3) % 4],
          }}
        />
      ))}
      <g fill="none" stroke="#0d0c11" strokeWidth="2.6" strokeLinejoin="round">
        {SWATCH_PANES.map((points, i) => (
          <polygon key={i} points={points} />
        ))}
      </g>
    </svg>
  ),
  pool: (
    <svg viewBox="0 0 160 80" preserveAspectRatio="xMidYMid slice">
      <defs>
        <filter id="st-swatch-caustic" x="-10%" y="-10%" width="120%" height="120%">
          <feTurbulence type="fractalNoise" baseFrequency=".045" numOctaves="1" seed="4" result="noise" />
          <feDisplacementMap in="SourceGraphic" in2="noise" scale="10" xChannelSelector="R" yChannelSelector="G" result="wavy" />
          <feGaussianBlur in="wavy" stdDeviation="1.6" result="glow" />
          <feMerge>
            <feMergeNode in="glow" />
            <feMergeNode in="wavy" />
          </feMerge>
        </filter>
      </defs>
      <g fill="none" stroke="rgba(255,255,255,.4)" strokeWidth="1.3" strokeLinejoin="round" filter="url(#st-swatch-caustic)">
        {SWATCH_PANES.map((points, i) => (
          <polygon key={i} points={points} />
        ))}
      </g>
    </svg>
  ),
  crystal: (
    <svg viewBox="0 0 160 80" preserveAspectRatio="xMidYMid slice">
      <g transform="scale(1.35)" stroke="rgba(255,255,255,.45)" strokeWidth=".55" strokeLinejoin="round">
        {SWATCH_PANES.map((points, i) => (
          <polygon key={i} points={points} fill={seeded(i + 240) > 0.5 ? "#fff" : "#000"} fillOpacity={0.05 + seeded(i + 280) * 0.18} />
        ))}
      </g>
    </svg>
  ),
  bokeh: (
    <svg viewBox="0 0 160 80" preserveAspectRatio="xMidYMid slice">
      {BOKEH_DISCS.map(({ x, y, r, accent }, i) => (
        <circle
          key={i}
          cx={x}
          cy={y}
          r={r}
          style={{ fill: accent, fillOpacity: 0.3, stroke: accent, strokeOpacity: 0.55, strokeWidth: 0.8 }}
        />
      ))}
    </svg>
  ),
};

function ArtworkSizeCard({
  size,
  coverUrl,
  onChange,
}: {
  size: number;
  coverUrl: string;
  onChange: (size: number) => void;
}) {
  // The preview takes the shape of this window, which is the screen it fills.
  const aspect = Math.min(2.4, Math.max(0.5, window.innerWidth / Math.max(1, window.innerHeight)));
  return (
    <Card title="Artwork size" sub="How big the cover is when you hide the player details with the eye button.">
      <div className="st-art">
        <div className="st-art-screen" style={{ aspectRatio: String(aspect), "--art-scale": size } as CSSProperties} aria-hidden="true">
          {coverUrl ? <img className="st-art-backdrop" src={coverUrl} alt="" /> : <span className="st-glass-liquid" />}
          <span className="st-art-cover">{coverUrl ? <img src={coverUrl} alt="" /> : null}</span>
        </div>
        <div className="st-control">
          <div className="st-row">
            <span className="st-row-title">Size</span>
            <span className="st-value">{size}%</span>
          </div>
          <Slider
            label="Artwork size"
            value={size}
            min={ARTWORK_SIZE.min}
            max={ARTWORK_SIZE.max}
            step={1}
            format={(value) => `${value}% of the screen`}
            onChange={onChange}
          />
          <span className="st-row-sub">Measured against the screen's shorter side. 100% runs edge to edge.</span>
        </div>
      </div>
    </Card>
  );
}

export function AppearanceSection({
  settings,
  coverUrl,
  onChange,
}: {
  settings: AppSettings;
  coverUrl: string;
  onChange: (patch: Partial<AppSettings>) => void;
}) {
  const glass = glassStyle(settings.glass_style);
  const low = clampNumber(settings.brightness_min, 1, 100, 20);
  const high = clampNumber(settings.brightness_max, low, 100, 85);
  const commandGap = clampNumber(settings.update_speed_ms, 150, 2000, RECOMMENDED_COMMAND_GAP_MS);

  return (
    <div className="st-stack">
      <Card title="Screen glass" sub="The pane the album's colors flow behind.">
        <div className="st-glass-options" role="radiogroup" aria-label="Screen glass">
          {GLASS_STYLES.map((value) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={glass === value}
              className="st-glass"
              onClick={() => onChange({ glass_style: value })}
            >
              <span className={`st-glass-preview st-glass-preview--${value}`} aria-hidden="true">
                <span className="st-glass-liquid" />
                {SWATCH_ART[value]}
              </span>
              <span className="st-glass-name">{GLASS_LABELS[value].name}</span>
              <span className="st-glass-sub">{GLASS_LABELS[value].sub}</span>
            </button>
          ))}
        </div>
      </Card>

      <ArtworkSizeCard
        size={artworkSize(settings.artwork_size)}
        coverUrl={coverUrl}
        onChange={(size) => onChange({ artwork_size: size })}
      />

      <Card
        title="Bulb brightness"
        sub="Under Still, lights stay between these levels; set both ends the same for a steady brightness. Wave, Pulse and Progress always use the full range."
      >
        <div className="st-control">
          <div className="st-row">
            <span className="st-row-title">Brightness</span>
            <span className="st-value">{low === high ? `${high}%` : `${low}% – ${high}%`}</span>
          </div>
          <RangeSlider
            label="Bulb brightness"
            low={low}
            high={high}
            min={1}
            max={100}
            format={(value) => `${value}%`}
            onChange={(nextLow, nextHigh) => onChange({ brightness_min: nextLow, brightness_max: nextHigh })}
          />
        </div>
      </Card>

      <Card>
        <Disclosure title="Advanced">
          <div className="st-control">
            <div className="st-row">
              <div className="st-row-text">
                <span className="st-row-title">Gap between commands to a bulb</span>
                <span className="st-row-sub">Raise it if bulbs miss changes. Lower it for snappier updates.</span>
              </div>
              <span className="st-value">
                {commandGap} ms{commandGap === RECOMMENDED_COMMAND_GAP_MS ? " (recommended)" : ""}
              </span>
            </div>
            <Slider
              label="Gap between commands to a bulb"
              value={commandGap}
              min={150}
              max={2000}
              step={50}
              format={(value) =>
                `${value} milliseconds${value === RECOMMENDED_COMMAND_GAP_MS ? ", recommended" : ""}`
              }
              onChange={(value) => onChange({ update_speed_ms: value })}
            />
          </div>
        </Disclosure>
      </Card>
    </div>
  );
}
