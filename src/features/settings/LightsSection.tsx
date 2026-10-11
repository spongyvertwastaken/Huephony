import { useState } from "react";
import { Check, Lightbulb, Plus, Search, Trash2, X } from "lucide-react";
import { discoverLights, identifyLight } from "../../lib/api";
import { type AppSettings, type DiscoveredLight, type LightCommandResult, type LightDevice, type LightDiscoveryResult } from "../../lib/types";
import { roomSizes, slotLabel } from "../lights/layout";
import { Notice, Button, Spinner, Card, Switch, SwitchRow, Disclosure, type NoticeState } from "./ui";
const IPV4_PATTERN = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

type IdentifyState = { busy: boolean; reachable?: boolean; message?: string };

function lightKey(light: { device_id?: string; ip?: string }) {
  return light.device_id ? `id:${light.device_id}` : `ip:${light.ip ?? ""}`;
}

function sameLight(saved: LightDevice, found: DiscoveredLight) {
  if (saved.device_id && found.device_id) return saved.device_id === found.device_id;
  return Boolean(saved.ip) && saved.ip === found.ip;
}

// Spread bulbs evenly through the color rotation, in list order.
function renumberLights(devices: LightDevice[]) {
  return devices.map((device, index) => ({ ...device, palette_index: index }));
}

function nextLightName(devices: LightDevice[]) {
  const taken = new Set(devices.map((device) => String(device.name ?? "").trim().toLowerCase()));
  let number = devices.length + 1;
  while (taken.has(`light ${number}`)) number += 1;
  return `Light ${number}`;
}

function newGoveeLight(found: DiscoveredLight, name: string): LightDevice {
  return {
    name,
    type: "bulb",
    brand: "Govee",
    ...(found.sku ? { sku: found.sku } : {}),
    adapter: "govee",
    device_id: found.device_id,
    ip: found.ip,
    enabled: true,
    palette_index: 0,
  };
}

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

export function LightsSection({
  settings,
  lightsOn,
  onToggleLights,
  onDevicesChange,
  onTestLights,
}: {
  settings: AppSettings;
  lightsOn: boolean;
  onToggleLights: () => void;
  onDevicesChange: (devices: LightDevice[]) => void;
  onTestLights: () => Promise<LightCommandResult>;
}) {
  const devices = settings.devices ?? [];
  const enabled = devices.filter((device) => device.enabled !== false).length;
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<LightCommandResult | null>(null);

  const status = !lightsOn
    ? "Off. Your lights keep whatever color they have now."
    : enabled
      ? `${plural(enabled, "light")} ${enabled === 1 ? "follows" : "follow"} the album artwork.`
      : "On, but no lights are added yet. Add them below.";

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await onTestLights());
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="st-stack">
      <Card hero>
        <SwitchRow title="Sync lights to music" sub={status} checked={lightsOn} onChange={onToggleLights} />
        <div className="st-divider" />
        <div className="st-row st-row--wrap">
          <span className="st-hint">Sets every light to a test color, so you can check they respond.</span>
          <Button onClick={runTest} disabled={testing || !devices.length} size="small">
            {testing ? <Spinner size={13} /> : <Lightbulb size={13} />}
            {testing ? "Testing..." : "Test lights"}
          </Button>
        </div>
        {test ? (
          <Notice tone={test.ok && test.connected ? "success" : "warning"} title={test.message}>
            {test.devices?.length ? (
              <ul className="st-diagnostics">
                {test.devices.map((device, index) => (
                  <li key={`${device.name}-${index}`}>
                    {device.ok ? <Check size={13} className="st-good" /> : <X size={13} className="st-warn" />}
                    <span>
                      <b>{device.name}</b>: {device.message}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </Notice>
        ) : null}
      </Card>
      <GoveeLights devices={devices} onDevicesChange={onDevicesChange} sizes={roomSizes(settings)} />
    </div>
  );
}

function GoveeSteps() {
  return (
    <ol className="st-steps">
      <li>
        <span>
          On your phone, open the <b>Govee Home</b> app, tap a bulb, tap the <b>gear icon</b> and turn on{" "}
          <b>LAN Control</b>. Do this for each bulb.
        </span>
      </li>
      <li>
        <span>
          Make sure this computer is on the <b>same Wi-Fi network</b> as the bulbs.
        </span>
      </li>
      <li>
        <span>
          Press <b>Find my lights</b> and add the bulbs it finds.
        </span>
      </li>
    </ol>
  );
}

function GoveeLights({
  devices,
  onDevicesChange,
  sizes,
}: {
  devices: LightDevice[];
  onDevicesChange: (devices: LightDevice[]) => void;
  sizes: number[];
}) {
  const slotCount = sizes.reduce((sum, size) => sum + size, 0);
  const [searching, setSearching] = useState(false);
  const [search, setSearch] = useState<LightDiscoveryResult | null>(null);
  const [notice, setNotice] = useState<NoticeState>(null);
  const [identify, setIdentify] = useState<Record<string, IdentifyState>>({});
  const [manualIp, setManualIp] = useState("");
  const [manualBusy, setManualBusy] = useState(false);
  const [manualMiss, setManualMiss] = useState("");
  const [jsonText, setJsonText] = useState("");
  const [jsonError, setJsonError] = useState("");

  const foundLights = search?.ok ? search.devices : [];
  const newLights = foundLights.filter((found) => !devices.some((saved) => sameLight(saved, found)));

  const addLights = (found: DiscoveredLight[]) => {
    const next = [...devices];
    for (const light of found) next.push(newGoveeLight(light, nextLightName(next)));
    onDevicesChange(renumberLights(next));
    setNotice({
      tone: "success",
      text:
        found.length === 1
          ? `Added ${next[next.length - 1].name}. Press Blink to see which bulb it is, then rename it.`
          : `Added ${found.length} lights. Press Blink on each to see which bulb it is, then rename it.`,
    });
  };

  const updateLight = (index: number, patch: Partial<LightDevice>) => {
    onDevicesChange(devices.map((device, current) => (current === index ? { ...device, ...patch } : device)));
  };

  const removeLight = (index: number) => {
    const removed = devices[index];
    onDevicesChange(renumberLights(devices.filter((_, current) => current !== index)));
    setNotice({ tone: "info", text: `Removed ${removed?.name || "the light"}.` });
  };

  const handleFind = async () => {
    setSearching(true);
    setNotice(null);
    const result = await discoverLights();
    setSearching(false);
    setSearch(result);
    // A saved bulb that the router gave a new address keeps working.
    let moved = 0;
    const updated = devices.map((saved) => {
      const match = result.devices.find((found) => saved.device_id && found.device_id === saved.device_id);
      if (!match || match.ip === saved.ip) return saved;
      moved += 1;
      return { ...saved, ip: match.ip, ...(match.sku && !saved.sku ? { sku: match.sku } : {}) };
    });
    if (moved) {
      onDevicesChange(updated);
      setNotice({ tone: "info", text: `Updated the network address of ${plural(moved, "saved light")}.` });
    }
  };

  const handleIdentify = async (light: { ip?: string; name?: string; device_id?: string }) => {
    const key = lightKey(light);
    if (!light.ip) {
      setIdentify((prev) => ({
        ...prev,
        [key]: { busy: false, reachable: false, message: "No IP address saved. Use Find my lights." },
      }));
      return;
    }
    setIdentify((prev) => ({ ...prev, [key]: { busy: true } }));
    const result = await identifyLight({ ip: light.ip, name: light.name, device_id: light.device_id });
    setIdentify((prev) => ({
      ...prev,
      [key]: { busy: false, reachable: result.ok && result.reachable, message: result.message },
    }));
  };

  const handleManualAdd = async (addWithoutAnswer = false) => {
    const ip = manualIp.trim();
    if (!IPV4_PATTERN.test(ip)) {
      setNotice({ tone: "error", text: "Enter an IP address like 192.168.1.50." });
      return;
    }
    if (devices.some((device) => device.ip === ip)) {
      setNotice({ tone: "info", text: `${ip} is already in your list.` });
      return;
    }
    if (addWithoutAnswer) {
      addLights([{ device_id: "", ip, sku: "", name: "" }]);
      setManualIp("");
      setManualMiss("");
      return;
    }
    setManualBusy(true);
    setManualMiss("");
    setNotice(null);
    const result = await discoverLights([ip]);
    setManualBusy(false);
    if (!result.ok) {
      setNotice({ tone: "error", text: result.message });
      return;
    }
    const fresh = result.devices.filter((found) => !devices.some((saved) => sameLight(saved, found)));
    if (fresh.length) {
      addLights(fresh);
      setManualIp("");
    } else if (result.devices.length) {
      setNotice({ tone: "info", text: "That light is already in your list." });
    } else {
      setManualMiss(ip);
    }
  };

  const applyJson = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(jsonText);
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : "Invalid JSON.");
      return;
    }
    if (
      !Array.isArray(parsed) ||
      !parsed.every((item) => item !== null && typeof item === "object" && !Array.isArray(item))
    ) {
      setJsonError("The light list must be a JSON array of objects.");
      return;
    }
    onDevicesChange(parsed as LightDevice[]);
    setJsonError("");
    setNotice({ tone: "success", text: "Light list updated from JSON." });
  };

  const identifyNote = (light: { device_id?: string; ip?: string }) => {
    const state = identify[lightKey(light)];
    if (!state || state.busy || !state.message) return null;
    return <span className={`st-device-meta ${state.reachable ? "st-good" : "st-warn"}`}>{state.message}</span>;
  };

  const blinkButton = (light: { device_id?: string; ip?: string; name?: string }) => {
    const busy = Boolean(identify[lightKey(light)]?.busy);
    return (
      <Button
        size="small"
        onClick={() => handleIdentify(light)}
        disabled={busy}
        title="Make this bulb blink so you can see which one it is"
      >
        {busy ? <Spinner size={13} /> : <Lightbulb size={13} />}
        {busy ? "Blinking..." : "Blink"}
      </Button>
    );
  };

  return (
    <Card
      title="Govee lights"
      sub={devices.length ? `${plural(devices.length, "light")} added. Changes save automatically.` : "Add your bulbs to get started."}
    >
      {devices.length ? (
        <Disclosure title="How to add more lights">
          <GoveeSteps />
        </Disclosure>
      ) : (
        <GoveeSteps />
      )}

      <div className="st-row st-row--start">
        <Button variant="primary" size="large" onClick={handleFind} disabled={searching}>
          {searching ? <Spinner size={16} /> : <Search size={16} />}
          {searching ? "Searching..." : "Find my lights"}
        </Button>
        {searching ? (
          <span className="st-hint">Looking for Govee lights on your network. This takes a few seconds.</span>
        ) : null}
      </div>

      {notice ? <Notice tone={notice.tone}>{notice.text}</Notice> : null}

      {search && !searching && !search.ok ? (
        <Notice tone="warning" title="Couldn't search for lights">
          {search.message ? <span>{search.message}</span> : null}
          {search.tips.length ? (
            <ul>
              {search.tips.map((tip) => (
                <li key={tip}>{tip}</li>
              ))}
            </ul>
          ) : null}
        </Notice>
      ) : null}

      {search && !searching && search.ok && !search.devices.length ? (
        <Notice tone="warning" title="No Govee lights found">
          <span>Try these, then press Find my lights again. You can also add a bulb by its IP address below.</span>
          {search.tips.length ? (
            <ul>
              {search.tips.map((tip) => (
                <li key={tip}>{tip}</li>
              ))}
            </ul>
          ) : null}
        </Notice>
      ) : null}

      {search && !searching && search.ok && search.devices.length > 0 && !newLights.length && !notice ? (
        <span className="st-hint">Every light found is already in your list.</span>
      ) : null}

      {newLights.length ? (
        <div className="st-list">
          <div className="st-list-head">
            <span>Found {plural(newLights.length, "new light")}</span>
            {newLights.length > 1 ? (
              <Button variant="primary" size="small" onClick={() => addLights(newLights)}>
                <Plus size={13} />
                Add all
              </Button>
            ) : null}
          </div>
          {newLights.map((found) => (
            <div key={lightKey(found)} className="st-device">
              <div className="st-device-main">
                <div className="st-row-text st-device-name">
                  <span className="st-row-title">{found.name}</span>
                  <span className="st-device-meta">{[found.sku, found.ip].filter(Boolean).join(" · ")}</span>
                </div>
                <div className="st-actions">
                  {blinkButton(found)}
                  <Button size="small" onClick={() => addLights([found])}>
                    <Plus size={13} />
                    Add
                  </Button>
                </div>
              </div>
              {identifyNote(found)}
            </div>
          ))}
        </div>
      ) : null}

      {devices.length ? (
        <div className="st-list">
          <div className="st-list-head">
            <span>Your lights</span>
          </div>
          {devices.map((device, index) => {
            const enabled = device.enabled !== false;
            const seen = foundLights.some((found) => sameLight(device, found));
            const details = [device.sku, device.ip || "no IP address"].filter(Boolean).join(" · ");
            return (
              <div key={`${lightKey(device)}-${index}`} className={`st-device${enabled ? "" : " st-device--off"}`}>
                <div className="st-device-main">
                  <Switch
                    checked={enabled}
                    onChange={(next) => updateLight(index, { enabled: next })}
                    label={`Use ${device.name || "this light"} for Spotify colors`}
                  />
                  <input
                    className="st-input st-input--compact st-device-name"
                    value={String(device.name ?? "")}
                    onChange={(event) => updateLight(index, { name: event.target.value })}
                    placeholder="Name"
                    aria-label="Light name"
                  />
                  <select
                    className="st-input st-input--compact st-device-spot"
                    value={typeof device.slot === "number" && device.slot < slotCount ? device.slot : ""}
                    onChange={(event) =>
                      updateLight(index, { slot: event.target.value === "" ? undefined : Number(event.target.value) })
                    }
                    aria-label="Where this light is"
                    title="Where this light is"
                  >
                    <option value="">Spot: automatic</option>
                    {Array.from({ length: slotCount }, (_, slot) => (
                      <option key={slot} value={slot}>
                        {slotLabel(sizes, slot)}
                      </option>
                    ))}
                  </select>
                  <div className="st-actions">
                    {blinkButton(device)}
                    <button
                      type="button"
                      className="st-icon-btn st-icon-btn--small"
                      onClick={() => removeLight(index)}
                      aria-label={`Remove ${device.name || "this light"}`}
                      title="Remove this light"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
                <span className="st-device-meta">
                  {details}
                  {search?.ok ? (
                    <span className={seen ? "st-good" : "st-warn"}>
                      {seen ? " · found on network" : " · not found in the last search"}
                    </span>
                  ) : null}
                </span>
                {identifyNote(device)}
              </div>
            );
          })}
        </div>
      ) : null}

      <div className="st-divider" />

      <Disclosure title="Can't find a bulb? Add it by its IP address">
        <span className="st-hint">
          Some routers block the search. Find the bulb's IP address in your router's list of connected devices (it may
          show up as “ihoment” or “Govee”), then enter it here.
        </span>
        <div className="st-actions">
          <input
            className="st-input st-input--compact st-mono"
            style={{ flex: "1 1 160px", width: "auto" }}
            value={manualIp}
            onChange={(event) => {
              setManualIp(event.target.value);
              setManualMiss("");
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") handleManualAdd();
            }}
            placeholder="192.168.1.50"
            aria-label="Bulb IP address"
            inputMode="decimal"
          />
          <Button onClick={() => handleManualAdd()} disabled={manualBusy}>
            {manualBusy ? <Spinner size={13} /> : <Plus size={13} />}
            {manualBusy ? "Checking..." : "Check and add"}
          </Button>
        </div>
        {manualMiss ? (
          <Notice tone="warning">
            <span>
              No Govee light answered at {manualMiss}. Check the address and that LAN Control is on. If you're sure
              it's right, add it anyway and press Blink to test it.
            </span>
            <div>
              <Button size="small" onClick={() => handleManualAdd(true)}>
                Add anyway
              </Button>
            </div>
          </Notice>
        ) : null}
      </Disclosure>

      <Disclosure
        title="Advanced: edit the light list as JSON"
        onToggle={(open) => {
          if (!open) return;
          setJsonText(JSON.stringify(devices, null, 2));
          setJsonError("");
        }}
      >
        <textarea
          className="st-input"
          value={jsonText}
          onChange={(event) => setJsonText(event.target.value)}
          rows={9}
          spellCheck={false}
          aria-label="Light list JSON"
        />
        <div className="st-row st-row--wrap">
          <span className={jsonError ? "st-hint st-warn" : "st-hint"}>
            {jsonError || "Each light needs an ip; device_id, name, enabled and palette_index are optional."}
          </span>
          <Button size="small" onClick={applyJson}>
            Apply JSON
          </Button>
        </div>
      </Disclosure>
    </Card>
  );
}
