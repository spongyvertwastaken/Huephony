import { useEffect, useRef, type MouseEvent as ReactMouseEvent } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { cursorPosition, getCurrentWindow, PhysicalPosition } from "@tauri-apps/api/window";
import { Maximize2, Minus, X } from "lucide-react";

// In fullscreen the top strip only drags once it's held or pulled, so a stray
// click on it can't knock the ambient view out of fullscreen.
const FULLSCREEN_DRAG_HOLD_MS = 300;
const FULLSCREEN_DRAG_MOVE_PX = 6;

export function DesktopWindowControls() {
  useEffect(() => {
    if (!isTauri()) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") restoreWindowed();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const restoreWindowed = async () => {
    if (!isTauri()) return;
    const appWindow = getCurrentWindow();
    await appWindow.setFullscreen(false).catch(() => undefined);
    await appWindow.unmaximize().catch(() => undefined);
  };

  const handleMinimize = () => {
    if (!isTauri()) return;
    getCurrentWindow().minimize().catch(() => undefined);
  };

  // Always goes fullscreen (hiding the taskbar), even from a snapped/maximized
  // window; Escape is the way back to a windowed size.
  const handleFullscreen = async () => {
    if (!isTauri()) return;
    const appWindow = getCurrentWindow();
    try {
      if (await appWindow.isMaximized()) await appWindow.unmaximize();
      await appWindow.setFullscreen(true);
    } catch {
      /* ignore */
    }
  };

  const handleClose = () => {
    if (!isTauri()) return;
    getCurrentWindow().close().catch(() => undefined);
  };

  // Drops out of fullscreen with the window under the cursor, then hands the
  // still-held mouse to the OS drag, like pulling down a maximized title bar.
  const dragOutOfFullscreen = async (fractionX: number, offsetY: number) => {
    const appWindow = getCurrentWindow();
    await appWindow.setFullscreen(false);
    // A window restored to maximized is moved by Windows' own drag. Otherwise
    // put it under the cursor at the same spot along its top edge, or the drag
    // would carry it from wherever it was last left.
    if (!(await appWindow.isMaximized())) {
      const [size, scale, cursor] = await Promise.all([
        appWindow.outerSize(),
        appWindow.scaleFactor(),
        cursorPosition(),
      ]);
      await appWindow.setPosition(
        new PhysicalPosition(Math.round(cursor.x - fractionX * size.width), Math.round(cursor.y - offsetY * scale)),
      );
    }
    await appWindow.startDragging();
  };

  const cancelHoldRef = useRef<() => void>();
  useEffect(() => () => cancelHoldRef.current?.(), []);

  const handleDrag = (event: ReactMouseEvent) => {
    if (!isTauri() || event.button !== 0) return;
    cancelHoldRef.current?.();
    const appWindow = getCurrentWindow();
    const { clientX, clientY } = event;
    const fractionX = clientX / window.innerWidth;

    // The release is watched from the first moment, so a click that ends
    // before the fullscreen check answers still counts as just a click.
    let pressed = true;
    let holdTimer: ReturnType<typeof setTimeout> | undefined;
    const onMove = (move: MouseEvent) => {
      if (Math.hypot(move.clientX - clientX, move.clientY - clientY) >= FULLSCREEN_DRAG_MOVE_PX) drag();
    };
    const release = () => {
      pressed = false;
      clearTimeout(holdTimer);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", release);
      cancelHoldRef.current = undefined;
    };
    const drag = () => {
      release();
      dragOutOfFullscreen(fractionX, clientY).catch(() => undefined);
    };
    window.addEventListener("mouseup", release);
    cancelHoldRef.current = release;

    appWindow
      .isFullscreen()
      .then((fullscreen) => {
        if (!pressed) return;
        if (!fullscreen) {
          release();
          return appWindow.startDragging();
        }
        window.addEventListener("mousemove", onMove);
        holdTimer = setTimeout(drag, FULLSCREEN_DRAG_HOLD_MS);
      })
      .catch(release);
  };

  return (
    <>
      {/* The window has no title bar, so an invisible strip along the top edge moves it. */}
      <div className="absolute top-0 left-0 right-0" style={{ height: 20, zIndex: 100 }} onMouseDown={handleDrag} />
      <div className="window-controls">
        <button className="icon-button" onClick={handleMinimize} aria-label="Minimize window" title="Minimize">
          <Minus size={17} />
        </button>
        <button className="icon-button" onClick={handleFullscreen} aria-label="Full screen" title="Full screen (Esc to exit)">
          <Maximize2 size={17} />
        </button>
        <button className="icon-button" onClick={handleClose} aria-label="Close window" title="Close">
          <X size={17} />
        </button>
      </div>
    </>
  );
}
