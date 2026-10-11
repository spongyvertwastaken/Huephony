import { useEffect, useRef, useState, type ReactNode } from "react";
import { Check, LampFloor, Lightbulb, Music2, ShieldCheck, Sparkles, TriangleAlert, X } from "lucide-react";
import { type AppSettings, type LightCommandResult, type NowPlayingState, type PlayerCommandResult } from "../../lib/types";
import { Spinner } from "./ui";
import { AnimatePresence, motion } from "motion/react";
import { accentStyle } from "./theme";
import { LightsSection } from "./LightsSection";
import { RoomSection } from "./RoomSection";
import { AppearanceSection } from "./AppearanceSection";
import { SpotifySection } from "./SpotifySection";
import { PrivacySection } from "./PrivacySection";
import "../../styles/settings.css";

export type SaveState = "idle" | "saving" | "saved" | "error";

type SectionId = "lights" | "room" | "appearance" | "spotify" | "privacy";

const SECTIONS: { id: SectionId; label: string; icon: typeof Lightbulb; blurb: string }[] = [
  { id: "lights", label: "Lights", icon: Lightbulb, blurb: "Your Govee bulbs, and whether they follow the music." },
  { id: "room", label: "Room", icon: LampFloor, blurb: "Where your bulbs stand, and how the light moves between them." },
  { id: "appearance", label: "Appearance", icon: Sparkles, blurb: "How the screen and the bulbs look while music plays." },
  { id: "spotify", label: "Spotify", icon: Music2, blurb: "The Spotify account this app follows." },
  { id: "privacy", label: "Privacy & legal", icon: ShieldCheck, blurb: "What the app keeps, where it goes, and the fine print." },
];
// Reopening Settings returns to the section that was open last.
let lastSection: SectionId = "lights";

function SaveStatus({ state, onRetry }: { state: SaveState; onRetry: () => void }) {
  if (state === "saving") {
    return (
      <span className="st-save" role="status">
        <Spinner size={12} />
        Saving...
      </span>
    );
  }
  if (state === "error") {
    return (
      <span className="st-save st-save--error" role="alert">
        <TriangleAlert size={12} />
        Couldn't save.
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      </span>
    );
  }
  return (
    <span className={`st-save${state === "saved" ? " st-save--saved" : ""}`} role="status">
      <Check size={12} />
      {state === "saved" ? "All changes saved" : "Saves automatically"}
    </span>
  );
}

export interface SettingsPanelProps {
  settings: AppSettings | null;
  loadMessage: string;
  nowPlaying: NowPlayingState | null;
  accent: [string, string];
  lightsOn: boolean;
  saveState: SaveState;
  onClose: () => void;
  onChange: (patch: Partial<AppSettings>) => void;
  onRetrySave: () => void;
  onToggleLights: () => void;
  onTestLights: () => Promise<LightCommandResult>;
  onSaveSpotify: (patch: Partial<AppSettings>) => Promise<void>;
  onConnectSpotify: () => void;
  onDisconnectSpotify: () => Promise<PlayerCommandResult>;
  onOpenLink: (url: string) => void;
}
export function SettingsPanel(props: SettingsPanelProps) {
  const { settings, nowPlaying, saveState } = props;
  const [section, setSection] = useState<SectionId>(lastSection);
  const panelRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(props.onClose);
  onCloseRef.current = props.onClose;
  const current = SECTIONS.find((item) => item.id === section) ?? SECTIONS[0];

  const openSection = (next: SectionId) => {
    lastSection = next;
    setSection(next);
    bodyRef.current?.scrollTo({ top: 0 });
  };

  // Escape closes; Tab stays inside the dialog.
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const focusFrame = requestAnimationFrame(() => panelRef.current?.focus());
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopImmediatePropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = panelRef.current;
      const controls = panel?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
      );
      if (!controls?.length) {
        event.preventDefault();
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      cancelAnimationFrame(focusFrame);
      window.removeEventListener("keydown", onKeyDown, true);
      previousFocus?.focus();
    };
  }, []);

  let content: ReactNode = null;
  if (settings) {
    if (section === "lights") {
      content = (
        <LightsSection
          settings={settings}
          lightsOn={props.lightsOn}
          onToggleLights={props.onToggleLights}
          onDevicesChange={(devices) => props.onChange({ devices })}
          onTestLights={props.onTestLights}
        />
      );
    } else if (section === "room") {
      content = (
        <RoomSection
          settings={settings}
          plan={nowPlaying?.track ? (nowPlaying.light_plan ?? []) : []}
          nowPlaying={nowPlaying}
          accent={props.accent}
          onChange={props.onChange}
          onOpenLights={() => openSection("lights")}
        />
      );
    } else if (section === "appearance") {
      content = (
        <AppearanceSection settings={settings} coverUrl={nowPlaying?.track?.cover_url ?? ""} onChange={props.onChange} />
      );
    } else if (section === "spotify") {
      content = (
        <SpotifySection
          settings={settings}
          nowPlaying={nowPlaying}
          onSave={props.onSaveSpotify}
          onConnect={props.onConnectSpotify}
          onDisconnect={props.onDisconnectSpotify}
          onOpenLink={props.onOpenLink}
        />
      );
    } else {
      content = (
        <PrivacySection settings={settings} onDisconnect={props.onDisconnectSpotify} onOpenLink={props.onOpenLink} />
      );
    }
  }

  return (
    <motion.div
      className="st-backdrop"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) props.onClose();
      }}
    >
      <motion.div
        ref={panelRef}
        className="st-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="st-title"
        tabIndex={-1}
        initial={{ opacity: 0, scale: 0.97, y: 14 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 8 }}
        transition={{ duration: 0.22, ease: [0.25, 0.46, 0.45, 0.94] }}
        style={accentStyle(props.accent)}
      >
        <nav className="st-nav" aria-label="Settings sections">
          <span id="st-title" className="st-nav-title">
            Settings
          </span>
          <div className="st-nav-list">
            {SECTIONS.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                className="st-nav-item"
                aria-current={section === id ? "page" : undefined}
                onClick={() => openSection(id)}
              >
                <Icon size={16} />
                <span>{label}</span>
              </button>
            ))}
          </div>
          <div className="st-nav-foot">
            <SaveStatus state={saveState} onRetry={props.onRetrySave} />
            <span className="st-version">Version {__APP_VERSION__}</span>
          </div>
        </nav>

        <div className="st-main">
          <header className="st-header">
            <div>
              <h2>{current.label}</h2>
              <p>{current.blurb}</p>
            </div>
            <button type="button" className="st-icon-btn" onClick={props.onClose} aria-label="Close settings">
              <X size={16} />
            </button>
          </header>
          <div className="st-body" ref={bodyRef}>
            {settings ? (
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={section}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.14 }}
                >
                  {content}
                </motion.div>
              </AnimatePresence>
            ) : (
              <div className="st-loading">
                <Spinner />
                {props.loadMessage || "Loading settings..."}
              </div>
            )}
          </div>
        </div>
      </motion.div>
    </motion.div>
  );
}
