import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Check, ChevronRight, CircleAlert, CircleCheck, Copy, Loader2, Minus, Plus, TriangleAlert } from "lucide-react";

export type Tone = "info" | "success" | "warning" | "error";
export type NoticeState = { tone: Tone; text: string } | null;

export function Notice({ tone, title, children }: { tone: Tone; title?: string; children?: ReactNode }) {
  const Icon = tone === "success" ? CircleCheck : tone === "info" ? CircleAlert : TriangleAlert;
  return (
    <div className={`st-notice st-notice--${tone}`} role={tone === "error" ? "alert" : "status"}>
      <Icon size={15} />
      <div className="st-notice-body">
        {title ? <span className="st-notice-title">{title}</span> : null}
        {children}
      </div>
    </div>
  );
}

export function Button({
  children,
  onClick,
  variant = "secondary",
  size,
  disabled,
  title,
}: {
  children: ReactNode;
  onClick: () => void;
  variant?: "primary" | "secondary" | "danger";
  size?: "small" | "large";
  disabled?: boolean;
  title?: string;
}) {
  const classes = ["st-btn", variant !== "secondary" && `st-btn--${variant}`, size && `st-btn--${size}`];
  return (
    <button type="button" className={classes.filter(Boolean).join(" ")} onClick={onClick} disabled={disabled} title={title}>
      {children}
    </button>
  );
}

export function Spinner({ size = 14 }: { size?: number }) {
  return <Loader2 size={size} className="animate-spin" />;
}

export function Card({
  title,
  sub,
  action,
  hero,
  children,
}: {
  title?: string;
  sub?: ReactNode;
  action?: ReactNode;
  hero?: boolean;
  children?: ReactNode;
}) {
  return (
    <section className={`st-card${hero ? " st-card--hero" : ""}`}>
      {title ? (
        <div className="st-card-head">
          <div>
            <h3 className="st-card-title">{title}</h3>
            {sub ? <p className="st-card-sub">{sub}</p> : null}
          </div>
          {action}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      className="st-switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
    />
  );
}

export function SwitchRow({
  title,
  sub,
  checked,
  onChange,
  disabled,
}: {
  title: string;
  sub: ReactNode;
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <div className="st-row">
      <div className="st-row-text">
        <span className="st-row-title">{title}</span>
        <span className="st-row-sub">{sub}</span>
      </div>
      <Switch checked={checked} onChange={onChange} label={title} disabled={disabled} />
    </div>
  );
}

export function Stepper({
  label,
  value,
  min,
  max,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onChange: (value: number) => void;
}) {
  return (
    <div className="st-row st-row--start">
      <span className="st-row-title">{label}</span>
      <div className="st-stepper">
        <button
          type="button"
          onClick={() => onChange(value - 1)}
          disabled={value <= min}
          aria-label={`Fewer ${label.toLowerCase()}`}
        >
          <Minus size={13} />
        </button>
        <output aria-live="polite" aria-label={label}>
          {value}
        </output>
        <button
          type="button"
          onClick={() => onChange(value + 1)}
          disabled={value >= max}
          aria-label={`More ${label.toLowerCase()}`}
        >
          <Plus size={13} />
        </button>
      </div>
    </div>
  );
}

// Thumbs are 16px wide; fills end at the thumb's center.
const thumbCenter = (fraction: number) => `calc(8px + ${Math.min(1, Math.max(0, fraction))} * (100% - 16px))`;

export function Slider({
  label,
  value,
  min,
  max,
  step,
  format,
  onChange,
  disabled,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  format: (value: number) => string;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  return (
    <input
      type="range"
      className="st-slider"
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      aria-label={label}
      aria-valuetext={format(value)}
      onChange={(event) => onChange(Number(event.target.value))}
      style={{ "--fill": thumbCenter((value - min) / (max - min)) } as CSSProperties}
    />
  );
}

export function RangeSlider({
  label,
  low,
  high,
  min,
  max,
  format,
  onChange,
}: {
  label: string;
  low: number;
  high: number;
  min: number;
  max: number;
  format: (value: number) => string;
  onChange: (low: number, high: number) => void;
}) {
  const fraction = (value: number) => (value - min) / (max - min);
  return (
    <div
      className="st-range"
      style={{ "--from": thumbCenter(fraction(low)), "--to": thumbCenter(fraction(high)) } as CSSProperties}
    >
      <div className="st-range-track">
        <div className="st-range-fill" />
      </div>
      <input
        type="range"
        min={min}
        max={max}
        value={low}
        aria-label={`${label}, lowest`}
        aria-valuetext={format(low)}
        onChange={(event) => onChange(Math.min(Number(event.target.value), high), high)}
        // When the thumbs meet near the top, the low thumb must be the one on
        // top, or it could never be dragged back down.
        style={{ zIndex: fraction(low) > 0.5 ? 3 : 1 }}
      />
      <input
        type="range"
        min={min}
        max={max}
        value={high}
        aria-label={`${label}, highest`}
        aria-valuetext={format(high)}
        onChange={(event) => onChange(low, Math.max(Number(event.target.value), low))}
        style={{ zIndex: 2 }}
      />
    </div>
  );
}

export function Disclosure({
  title,
  defaultOpen = false,
  onToggle,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  onToggle?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="st-disclosure">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
          onToggle?.(!open);
        }}
      >
        <ChevronRight size={14} />
        {title}
      </button>
      {open ? <div className="st-disclosure-body">{children}</div> : null}
    </div>
  );
}

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1600);
    } catch {
      /* The text stays selectable for copying by hand. */
    }
  };
  return (
    <button type="button" className="st-icon-btn st-icon-btn--small" onClick={copy} aria-label={label} title={label}>
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </button>
  );
}
