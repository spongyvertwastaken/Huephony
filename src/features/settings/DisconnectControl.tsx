import { useState } from "react";
import { Unplug } from "lucide-react";
import { type PlayerCommandResult } from "../../lib/types";
import { Notice, Button, Spinner, type NoticeState } from "./ui";

export function DisconnectControl({
  onDisconnect,
  label = "Disconnect",
}: {
  onDisconnect: () => Promise<PlayerCommandResult>;
  label?: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<NoticeState>(null);

  const run = async () => {
    setBusy(true);
    const outcome = await onDisconnect();
    setBusy(false);
    setConfirming(false);
    setResult({ tone: outcome.ok ? "success" : "error", text: outcome.message });
  };

  if (confirming) {
    return (
      <div className="st-confirm" role="group" aria-label="Confirm disconnect">
        <span>
          Disconnect Spotify? This deletes your Spotify sign-in, the current playback information and the app's logs
          from this computer. Your settings and lights stay.
        </span>
        <div className="st-actions">
          <Button variant="danger" onClick={run} disabled={busy}>
            {busy ? <Spinner size={13} /> : <Unplug size={13} />}
            Disconnect
          </Button>
          <Button onClick={() => setConfirming(false)} disabled={busy}>
            Cancel
          </Button>
        </div>
      </div>
    );
  }
  return (
    <>
      <div className="st-actions">
        <Button
          variant="danger"
          onClick={() => {
            setResult(null);
            setConfirming(true);
          }}
        >
          <Unplug size={13} />
          {label}
        </Button>
      </div>
      {result ? <Notice tone={result.tone}>{result.text}</Notice> : null}
    </>
  );
}
