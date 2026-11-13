import { ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState, createContext, useContext, useCallback } from "react";

// ----------------------------------------------------------------- icons
export function Icon({ name, fill, className = "" }: { name: string; fill?: boolean; className?: string }) {
  return <span className={`icon ${fill ? "fill" : ""} ${className}`} aria-hidden="true">{name}</span>;
}

// ---------------------------------------------------- wavy progress (M3 Expressive)
export function Wavy({ value, indeterminate, tone = "primary" }: { value: number; indeterminate?: boolean; tone?: "primary" | "success" | "error" }) {
  const d = useMemo(() => {
    let p = "M0 7";
    for (let x = 0; x < 2400; x += 20) p += " q5 -6 10 0 t10 0";
    return p;
  }, []);
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className={`wavy ${tone} ${indeterminate ? "indet" : ""}`} role="progressbar" aria-valuenow={indeterminate ? undefined : Math.round(v)}>
      <svg className="wavy-track" height="14" width="100%"><line x1="0" y1="7" x2="100%" y2="7" /></svg>
      <div className="wavy-active" style={indeterminate ? undefined : { clipPath: `inset(0 ${100 - v}% 0 0)` }}>
        <svg height="14" width="2400" className="wavy-path"><path d={d} /></svg>
      </div>
    </div>
  );
}

/** M3 Expressive loading indicator: a shape that keeps morphing. */
export function Loader({ label }: { label?: string }) {
  return (
    <div className="center">
      <div className="shape-loader" aria-label={label ?? "Loading"} />
    </div>
  );
}

// ---------------------------------------------------------------- toasts
type ToastKind = "info" | "error" | "success";
const ToastCtx = createContext<(msg: string, kind?: ToastKind) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [t, setT] = useState<{ id: number; msg: string; kind: ToastKind } | null>(null);
  const timer = useRef<number>(0);
  const push = useCallback((msg: string, kind: ToastKind = "info") => {
    window.clearTimeout(timer.current);
    setT({ id: Date.now(), msg, kind });
    timer.current = window.setTimeout(() => setT(null), kind === "error" ? 6000 : 3500);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      {t && (
        <div key={t.id} className={`snack ${t.kind}`} role="status" aria-live="polite">
          <Icon name={t.kind === "error" ? "error" : t.kind === "success" ? "check_circle" : "info"} fill />
          <span>{t.msg}</span>
        </div>
      )}
    </ToastCtx.Provider>
  );
}

// ---------------------------------------------------------------- dialog
export function Dialog({ title, onClose, children, actions, wide }: { title: string; onClose: () => void; children: ReactNode; actions?: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`dialog ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        <div className="dialog-body">{children}</div>
        {actions && <div className="row">{actions}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------ text field
export function Field({ label, value, onChange, type = "text", autoFocus, autoComplete, hint, onEnter, trailing, maxLength }: {
  label: string; value: string; onChange: (v: string) => void; type?: string; autoFocus?: boolean; autoComplete?: string; hint?: string;
  onEnter?: () => void; trailing?: ReactNode; maxLength?: number;
}) {
  return (
    <div className="tf">
      <label>
        <input
          value={value} type={type} placeholder=" " autoFocus={autoFocus} autoComplete={autoComplete} maxLength={maxLength}
          onChange={(e) => onChange(e.target.value)} onKeyDown={(e) => e.key === "Enter" && onEnter?.()}
        />
        <span className="tf-label">{label}</span>
      </label>
      {trailing && <span className="tf-trail">{trailing}</span>}
      {hint && <small>{hint}</small>}
    </div>
  );
}

export function PasswordField(props: Omit<Parameters<typeof Field>[0], "type" | "trailing">) {
  const [show, setShow] = useState(false);
  return (
    <Field
      {...props} type={show ? "text" : "password"}
      trailing={<button type="button" className="icon-btn" onClick={() => setShow((s) => !s)} aria-label={show ? "Hide password" : "Show password"}><Icon name={show ? "visibility_off" : "visibility"} /></button>}
    />
  );
}

// ------------------------------------------------------------- popup menu
export interface MenuItem { label: string; icon?: string; onClick?: () => void; danger?: boolean; divider?: boolean; disabled?: boolean }

export function Menu({ x, y, items, onClose }: { x: number; y: number; items: MenuItem[]; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: x, top: y });
  useLayoutEffect(() => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    setPos({ left: Math.max(8, Math.min(x, window.innerWidth - r.width - 8)), top: Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) });
  }, [x, y]);
  useEffect(() => {
    const close = () => onClose();
    const k = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", k);
    return () => { window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); window.removeEventListener("keydown", k); };
  }, [onClose]);
  return (
    <div className="menu-scrim" onMouseDown={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }}>
      <div ref={ref} className="menu" style={pos} onMouseDown={(e) => e.stopPropagation()} role="menu">
        {items.map((it, i) =>
          it.divider ? <hr key={i} /> : (
            <button key={i} role="menuitem" disabled={it.disabled} className={it.danger ? "danger" : ""} onClick={() => { onClose(); it.onClick?.(); }}>
              {it.icon && <Icon name={it.icon} />}<span>{it.label}</span>
            </button>
          ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ theme
export function useTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(() => {
    try { const s = localStorage.getItem("gitdrive.theme"); if (s) return s; } catch { /* ignore */ }
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#141218" : "#fef7ff");
    try { localStorage.setItem("gitdrive.theme", theme); } catch { /* ignore */ }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === "dark" ? "light" : "dark"))];
}

export function ThemeButton() {
  const [theme, toggle] = useTheme();
  return (
    <button className="icon-btn" onClick={toggle} aria-label="Toggle dark mode" title="Toggle dark mode">
      <Icon name={theme === "dark" ? "light_mode" : "dark_mode"} />
    </button>
  );
}

export function copyText(text: string): Promise<boolean> {
  return navigator.clipboard?.writeText(text).then(() => true, () => false) ?? Promise.resolve(false);
}

export function Avatar({ name, size = 40 }: { name: string; size?: number }) {
  const hue = [...name].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);
  return (
    <span className="avatar" style={{ width: size, height: size, fontSize: size * 0.45, background: `hsl(${hue} 55% 82%)`, color: `hsl(${hue} 45% 24%)` }}>
      {name.slice(0, 1).toUpperCase()}
    </span>
  );
}
