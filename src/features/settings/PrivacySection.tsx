import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ChevronRight, ExternalLink, FileText, HardDrive, Lock, Scale, ShieldCheck, Wifi } from "lucide-react";
import { type AppSettings, type PlayerCommandResult } from "../../lib/types";
import { Button, Card, CopyButton, Notice, Spinner } from "./ui";
import { Markdown } from "../../components/Markdown";
import { DisconnectControl } from "./DisconnectControl";
import { SPOTIFY_APPS_URL } from "./constants";

const DOCUMENTS = {
  privacy: {
    title: "Privacy policy",
    sub: "What the app accesses, keeps and shares.",
    icon: ShieldCheck,
    load: () => import("../../app/content/legal").then((content) => content.privacyPolicy),
  },
  terms: {
    title: "Terms of use",
    sub: "Using the app, safety, and warranties.",
    icon: Scale,
    load: () => import("../../app/content/legal").then((content) => content.termsOfUse),
  },
  notices: {
    title: "Open-source notices",
    sub: "The software the app is built with.",
    icon: FileText,
    load: () => import("../../app/content/third-party-notices.txt?raw").then((content) => content.default),
  },
} as const;

type DocumentId = keyof typeof DOCUMENTS;
type DocumentLoad = {
  id: DocumentId;
  source: string | null;
  failed: boolean;
};

export function PrivacySection({
  settings,
  onDisconnect,
  onOpenLink,
}: {
  settings: AppSettings;
  onDisconnect: () => Promise<PlayerCommandResult>;
  onOpenLink: (url: string) => void;
}) {
  const [openDoc, setOpenDoc] = useState<DocumentId | null>(null);
  const [loadedDoc, setLoadedDoc] = useState<DocumentLoad | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const topRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    topRef.current?.closest(".st-body")?.scrollTo({ top: 0 });
  }, [openDoc]);

  useEffect(() => {
    if (!openDoc) return;
    let active = true;
    setLoadedDoc({ id: openDoc, source: null, failed: false });
    DOCUMENTS[openDoc].load().then(
      (source) => {
        if (active) setLoadedDoc({ id: openDoc, source, failed: false });
      },
      () => {
        if (active) setLoadedDoc({ id: openDoc, source: null, failed: true });
      },
    );
    return () => {
      active = false;
    };
  }, [openDoc, loadAttempt]);

  if (openDoc) {
    const document = loadedDoc?.id === openDoc ? loadedDoc : null;
    return (
      <div className="st-stack" ref={topRef}>
        <button type="button" className="st-back" onClick={() => setOpenDoc(null)}>
          <ArrowLeft size={14} />
          Privacy & legal
        </button>
        {document?.failed ? (
          <Notice tone="error" title="Couldn't open this document.">
            <Button size="small" onClick={() => setLoadAttempt((attempt) => attempt + 1)}>
              Try again
            </Button>
          </Notice>
        ) : document?.source !== null && document?.source !== undefined ? (
          <Markdown source={document.source} onOpenLink={onOpenLink} />
        ) : (
          <div className="st-loading" role="status">
            <Spinner />
            Loading {DOCUMENTS[openDoc].title.toLowerCase()}...
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="st-stack" ref={topRef}>
      <Card hero>
        <div className="st-facts">
          <div className="st-fact">
            <span className="st-fact-icon">
              <HardDrive size={16} />
            </span>
            <div className="st-row-text">
              <span className="st-row-title">Your data stays on this computer</span>
              <span className="st-row-sub">
                Settings, your Spotify sign-in and logs are stored only here. The app's developer never receives any of it.
              </span>
            </div>
          </div>
          <div className="st-fact">
            <span className="st-fact-icon">
              <Wifi size={16} />
            </span>
            <div className="st-row-text">
              <span className="st-row-title">Talks only to Spotify and your lights</span>
              <span className="st-row-sub">No ads, analytics, tracking or crash reports. Light commands stay on your home network.</span>
            </div>
          </div>
          <div className="st-fact">
            <span className="st-fact-icon">
              <Lock size={16} />
            </span>
            <div className="st-row-text">
              <span className="st-row-title">Sign-in stays on this PC</span>
              <span className="st-row-sub">
                The app uses Windows Data Protection for your Spotify secret and sign-in when available. Storage details
                are explained in the privacy policy below.
              </span>
            </div>
          </div>
        </div>
      </Card>

      <Card title="Your data" sub="Where the app keeps its files, and how to delete them.">
        <div className="st-field">
          <span className="st-label">Data folder</span>
          <div className="st-actions" style={{ flexWrap: "nowrap" }}>
            <span className="st-path st-mono">{settings.config_dir || "%APPDATA%\\SpotifyRoomLighting"}</span>
            <CopyButton text={settings.config_dir || "%APPDATA%\\SpotifyRoomLighting"} label="Copy folder path" />
          </div>
        </div>
        <div className="st-divider" />
        <div className="st-row-text">
          <span className="st-row-title">Disconnect Spotify and delete its data</span>
          <span className="st-row-sub">
            Removes your sign-in, the current playback information and the logs. You can also remove the app's access in{" "}
            <button type="button" className="st-link" onClick={() => onOpenLink(SPOTIFY_APPS_URL)}>
              your Spotify account
              <ExternalLink size={11} />
            </button>
            .
          </span>
        </div>
        <DisconnectControl onDisconnect={onDisconnect} label="Disconnect and delete" />
      </Card>

      <Card title="Documents">
        <div className="st-doc-links">
          {(Object.keys(DOCUMENTS) as DocumentId[]).map((id) => {
            const { title, sub, icon: Icon } = DOCUMENTS[id];
            return (
              <button key={id} type="button" className="st-doc-link" onClick={() => setOpenDoc(id)}>
                <Icon size={17} />
                <span className="st-row-text">
                  <span className="st-row-title">{title}</span>
                  <span className="st-row-sub">{sub}</span>
                </span>
                <ChevronRight size={16} />
              </button>
            );
          })}
        </div>
      </Card>

      <p className="st-fineprint">
        Huephony is an independent app. It is not affiliated with, sponsored by or endorsed by Spotify or
        Govee. Spotify is a trademark of Spotify AB. Govee is a trademark of its owner.
        <br />
        Version {__APP_VERSION__}
      </p>
    </div>
  );
}
