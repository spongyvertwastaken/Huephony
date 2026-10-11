import { useState } from "react";
import { ExternalLink, Eye, EyeOff, Lock, Music2 } from "lucide-react";
import { type AppSettings, type NowPlayingState, type PlayerCommandResult } from "../../lib/types";
import { Notice, Button, Spinner, Card, Disclosure, CopyButton, type NoticeState } from "./ui";
import { DisconnectControl } from "./DisconnectControl";
import { SPOTIFY_APPS_URL, SPOTIFY_DASHBOARD_URL } from "./constants";

export function SpotifySection({
  settings,
  nowPlaying,
  onSave,
  onConnect,
  onDisconnect,
  onOpenLink,
}: {
  settings: AppSettings;
  nowPlaying: NowPlayingState | null;
  onSave: (patch: Partial<AppSettings>) => Promise<void>;
  onConnect: () => void;
  onDisconnect: () => Promise<PlayerCommandResult>;
  onOpenLink: (url: string) => void;
}) {
  const savedSecret = Boolean(settings.spotify_client_secret);
  const [clientId, setClientId] = useState(settings.spotify_client_id ?? "");
  const [secret, setSecret] = useState("");
  const [showSecret, setShowSecret] = useState(false);
  const [redirectUri, setRedirectUri] = useState(settings.spotify_redirect_uri ?? "");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<NoticeState>(null);

  const connected = Boolean(nowPlaying?.authenticated);
  const configured = Boolean(settings.spotify_client_id && savedSecret);
  const track = nowPlaying?.track;
  const dirty =
    clientId.trim() !== (settings.spotify_client_id ?? "") ||
    secret.trim() !== "" ||
    redirectUri.trim() !== (settings.spotify_redirect_uri ?? "");
  const canSave = dirty && Boolean(clientId.trim()) && Boolean(secret.trim() || savedSecret) && Boolean(redirectUri.trim());

  const save = async () => {
    setBusy(true);
    setNotice(null);
    try {
      await onSave({
        spotify_client_id: clientId.trim(),
        spotify_redirect_uri: redirectUri.trim(),
        ...(secret.trim() ? { spotify_client_secret: secret.trim() } : {}),
      });
      setSecret("");
      setNotice({ tone: "success", text: "Saved. Finish connecting in the browser window that opened." });
      onConnect();
    } catch (error) {
      setNotice({ tone: "error", text: error instanceof Error ? error.message : "Could not save. Try again." });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="st-stack">
      <Card hero>
        <div className="st-row st-row--wrap">
          <div className="st-row-text">
            <span className="st-status">
              <span className={`st-dot${connected ? " st-dot--on" : configured ? " st-dot--warn" : ""}`} />
              {connected ? "Connected" : configured ? "Not connected" : "Not set up"}
            </span>
            <span className="st-row-sub">
              {connected
                ? track
                  ? `Now playing: ${track.title} · ${track.artist}`
                  : "Nothing is playing right now."
                : configured
                  ? "Connect to show what's playing and color your lights."
                  : "Add your Spotify developer app below, then connect."}
            </span>
          </div>
          {configured ? (
            <Button variant={connected ? "secondary" : "primary"} onClick={onConnect}>
              <Music2 size={14} />
              {connected ? "Reconnect" : "Connect Spotify"}
            </Button>
          ) : null}
        </div>
        {connected ? (
          <>
            <div className="st-divider" />
            <DisconnectControl onDisconnect={onDisconnect} />
          </>
        ) : null}
      </Card>

      <Card title="Developer app" sub="The app connects through a Spotify developer app that you own. It takes a couple of minutes to create.">
        <ol className="st-steps">
          <li>
            <span>
              Open the{" "}
              <button type="button" className="st-link" onClick={() => onOpenLink(SPOTIFY_DASHBOARD_URL)}>
                Spotify Developer Dashboard
                <ExternalLink size={12} />
              </button>{" "}
              and create an app. Choose <b>Web API</b> when asked.
            </span>
          </li>
          <li>
            <span>
              Add <b className="st-mono">{redirectUri || "http://127.0.0.1:8000/callback"}</b> as a Redirect URI.
            </span>
          </li>
          <li>
            <span>Paste the app's Client ID and Client Secret below, then press Save and connect.</span>
          </li>
        </ol>
        <div className="st-grid-2">
          <label className="st-field">
            <span className="st-label">Client ID</span>
            <input
              className="st-input st-mono"
              value={clientId}
              onChange={(event) => setClientId(event.target.value)}
              spellCheck={false}
              autoComplete="off"
            />
          </label>
          <label className="st-field">
            <span className="st-label">Client Secret</span>
            <span className="st-input-wrap">
              <input
                className="st-input st-mono"
                type={showSecret ? "text" : "password"}
                value={secret}
                onChange={(event) => setSecret(event.target.value)}
                placeholder={savedSecret ? "Saved. Type to replace it." : ""}
                spellCheck={false}
                autoComplete="off"
              />
              <button
                type="button"
                className="st-icon-btn st-icon-btn--small"
                onClick={() => setShowSecret(!showSecret)}
                aria-label={showSecret ? "Hide secret" : "Show secret"}
                title={showSecret ? "Hide" : "Show"}
              >
                {showSecret ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </span>
          </label>
        </div>
        <Disclosure title="Redirect URI">
          <label className="st-field">
            <span className="st-label">Must match the one in your developer app exactly.</span>
            <span className="st-input-wrap">
              <input
                className="st-input st-mono"
                value={redirectUri}
                onChange={(event) => setRedirectUri(event.target.value)}
                spellCheck={false}
                autoComplete="off"
              />
              <CopyButton text={redirectUri} label="Copy redirect URI" />
            </span>
          </label>
        </Disclosure>
        {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}
        <div className="st-row st-row--wrap">
          <span className="st-hint">
            <Lock size={11} style={{ display: "inline", marginRight: 4, verticalAlign: -1 }} />
            The secret stays on this computer. Changing these details signs you out of Spotify.
          </span>
          <Button variant="primary" onClick={save} disabled={!canSave || busy}>
            {busy ? <Spinner size={13} /> : null}
            Save and connect
          </Button>
        </div>
      </Card>

      <p className="st-fineprint">
        Playback controls need Spotify Premium. To remove this app's access from your account at any time, visit{" "}
        <button type="button" className="st-link" onClick={() => onOpenLink(SPOTIFY_APPS_URL)}>
          spotify.com/account/apps
          <ExternalLink size={11} />
        </button>
        .
      </p>
    </div>
  );
}
