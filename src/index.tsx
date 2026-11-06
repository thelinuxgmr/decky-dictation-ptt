import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { call, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { FaMicrophone } from "react-icons/fa";

interface DictationStatus {
  running: boolean;
  setup: string; // not_started | downloading | extracting | ready | error
  detail: string;
}

function statusText(status: DictationStatus | null, running: boolean): string {
  if (running) return "Listening… speak and the text is typed into the focused app.";
  if (!status) return "Push-to-talk dictation (fully offline). Text goes to whatever has focus.";
  switch (status.setup) {
    case "downloading":
      return "First-run setup: downloading the speech engine and model (one time, ~50 MB)…";
    case "extracting":
      return "First-run setup: unpacking the speech model…";
    case "ready":
      return "Ready. Toggle on, or hold L4 + R4 together, wait a second, then speak.";
    case "error":
      return `Setup problem: ${status.detail || "check the plugin log."}`;
    default:
      return "Push-to-talk dictation (fully offline). First use downloads the speech model once.";
  }
}

function Content() {
  const [running, setRunning] = useState<boolean>(false);
  const [status, setStatus] = useState<DictationStatus | null>(null);
  const [busy, setBusy] = useState<boolean>(false);

  const refresh = async () => {
    try {
      const s = await call<[], DictationStatus>("dictation_status");
      setStatus(s);
      setRunning(s.running);
    } catch {
      /* backend not ready yet */
    }
  };

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, []);

  const onToggle = async (value: boolean) => {
    setBusy(true);
    try {
      const now = value
        ? await call<[], boolean>("start_dictation")
        : await call<[], boolean>("stop_dictation");
      setRunning(now);
    } finally {
      setBusy(false);
    }
  };

  return (
    <PanelSection title="Voice Dictation">
      <PanelSectionRow>
        <ToggleField
          label="Dictation"
          description={statusText(status, running)}
          checked={running}
          disabled={busy}
          onChange={onToggle}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ fontSize: "12px", lineHeight: "1.45" }}>
          Push-to-talk: focus the text box you want to type in, hold <b>L4 + R4</b>{" "}
          together, wait one or two seconds, then start speaking — that way it
          captures everything. Let go of the paddles to stop.
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}

export default definePlugin(() => ({
  name: "Decky Dictation",
  content: <Content />,
  icon: <FaMicrophone />,
}));
