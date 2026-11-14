import { useState, useSyncExternalStore } from "react";
import { fmtBytes } from "../api";
import { uploads } from "../upload";
import { Icon, Wavy } from "./ui";

export default function UploadTray() {
  const jobs = useSyncExternalStore(uploads.subscribe, uploads.snapshot);
  const [open, setOpen] = useState(true);
  if (!jobs.length) return null;
  const running = jobs.filter((j) => ["queued", "uploading", "finalizing"].includes(j.status));
  const total = jobs.reduce((a, j) => a + j.size, 0) || 1;
  const sent = jobs.reduce((a, j) => a + (j.status === "done" ? j.size : j.sent), 0);
  const title = running.length ? `Uploading ${running.length} item${running.length === 1 ? "" : "s"}` : `${jobs.filter((j) => j.status === "done").length} upload${jobs.length === 1 ? "" : "s"} complete`;
  return (
    <aside className={`tray ${open ? "open" : ""}`} aria-label="Uploads">
      <header onClick={() => setOpen((o) => !o)}>
        <b>{title}</b>
        <span className="grow" />
        <button className="icon-btn" aria-label={open ? "Collapse" : "Expand"}><Icon name={open ? "expand_more" : "expand_less"} /></button>
        <button className="icon-btn" aria-label="Clear finished" onClick={(e) => { e.stopPropagation(); uploads.clearFinished(); }}><Icon name="close" /></button>
      </header>
      {running.length > 0 && <Wavy value={(sent / total) * 100} />}
      {open && (
        <ul>
          {jobs.slice().reverse().map((j) => (
            <li key={j.id} className={j.status}>
              <Icon name={j.status === "done" ? "check_circle" : j.status === "error" ? "error" : j.status === "cancelled" ? "cancel" : "upload_file"} fill={j.status !== "uploading"} className="state" />
              <div className="meta">
                <div className="name" title={j.path}>{j.name}</div>
                <div className="sub">
                  {j.status === "uploading" ? `${fmtBytes(j.sent)} of ${fmtBytes(j.size)}` : j.status === "finalizing" ? "Saving to GitHub…"
                    : j.status === "queued" ? "Waiting…" : j.status === "done" ? `${fmtBytes(j.size)} · saved to ${j.spaceName}` : j.status === "cancelled" ? "Cancelled" : j.error}
                </div>
                {(j.status === "uploading" || j.status === "finalizing") && <Wavy value={j.status === "finalizing" ? 100 : (j.sent / (j.size || 1)) * 100} indeterminate={j.status === "finalizing"} />}
              </div>
              {(j.status === "uploading" || j.status === "queued") && <button className="icon-btn" aria-label="Cancel" onClick={() => uploads.cancel(j.id)}><Icon name="close" /></button>}
              {j.status === "error" && <button className="icon-btn" aria-label="Retry" onClick={() => uploads.retry(j.id)}><Icon name="refresh" /></button>}
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
