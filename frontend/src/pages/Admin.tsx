import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, Navigate } from "react-router-dom";
import { AdminOverview, AdminSpace, AdminUser, api, fmtAgo, fmtBytes, fmtDate } from "../api";
import { useAuth } from "../auth";
import { Avatar, Dialog, Field, Icon, Loader, Menu, MenuItem, ThemeButton, copyText, useToast } from "../components/ui";
import { ConfirmDialog } from "../components/dialogs";

export default function Admin() {
  const { user } = useAuth();
  const toast = useToast();
  const [tab, setTab] = useState<"overview" | "users" | "spaces">("overview");
  const [ov, setOv] = useState<AdminOverview | null>(null);
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [spaces, setSpaces] = useState<AdminSpace[] | null>(null);
  const [filter, setFilter] = useState("");
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const [dlg, setDlg] = useState<{ k: "quota" | "delete" | "link"; u: AdminUser; link?: string } | null>(null);
  const [gb, setGb] = useState("");

  const load = useCallback(() => {
    const fail = (e: Error) => toast(e.message, "error");
    api.admin.overview().then(setOv).catch(fail);
    api.admin.users().then(setUsers).catch(fail);
    api.admin.spaces().then(setSpaces).catch(fail);
  }, [toast]);
  useEffect(() => { if (user?.is_admin) load(); }, [user, load]);

  const maxBytes = useMemo(() => Math.max(1, ...(users ?? []).map((u) => u.bytes)), [users]);
  if (!user?.is_admin) return <Navigate to="/app" replace />;

  const list = (users ?? []).filter((u) => !filter || u.username.includes(filter.toLowerCase()) || (u.email ?? "").toLowerCase().includes(filter.toLowerCase()));
  const act = async (fn: () => Promise<unknown>, ok: string) => { try { await fn(); toast(ok, "success"); load(); } catch (e) { toast((e as Error).message, "error"); } };

  const userMenu = (e: React.MouseEvent, u: AdminUser) => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setMenu({ x: r.right - 230, y: r.bottom, items: [
      { label: u.disabled ? "Enable account" : "Disable account", icon: u.disabled ? "check_circle" : "block", disabled: u.username === user.username, onClick: () => act(() => api.admin.patchUser(u.username, { disabled: !u.disabled }), u.disabled ? "Account enabled" : "Account disabled") },
      { label: "Set storage quota", icon: "data_usage", onClick: () => { setGb(u.quota_bytes ? String(u.quota_bytes / 1024 ** 3) : ""); setDlg({ k: "quota", u }); } },
      { label: "Generate reset link", icon: "key", onClick: async () => { try { setDlg({ k: "link", u, link: (await api.admin.resetLink(u.username)).link }); } catch (er) { toast((er as Error).message, "error"); } } },
      { divider: true, label: "" },
      { label: "Delete user and data", icon: "delete_forever", danger: true, disabled: u.is_admin, onClick: () => setDlg({ k: "delete", u }) },
    ] });
  };

  return (
    <div className="admin">
      <header className="l-nav">
        <Link to="/app" className="btn text"><Icon name="arrow_back" /> Back to drive</Link>
        <b className="admin-title"><Icon name="admin_panel_settings" fill /> Admin</b>
        <nav><button className="icon-btn" onClick={load} aria-label="Refresh"><Icon name="refresh" /></button><ThemeButton /></nav>
      </header>
      <div className="tabs" role="tablist">
        {(["overview", "users", "spaces"] as const).map((t) => <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t[0].toUpperCase() + t.slice(1)}</button>)}
      </div>

      {tab === "overview" && (!ov ? <Loader /> : (
        <>
          <div className="stat-grid">
            <Stat icon="group" label="Users" value={String(ov.users)} sub={ov.disabled_users ? `${ov.disabled_users} disabled` : "all active"} tone="primary" />
            <Stat icon="description" label="Files" value={ov.files.toLocaleString()} sub={`${ov.folders.toLocaleString()} folders`} tone="tertiary" />
            <Stat icon="database" label="Stored" value={fmtBytes(ov.bytes)} sub="before encryption overhead" tone="secondary" />
            <Stat icon="workspaces" label="Spaces" value={String((ov.spaces.shared ?? 0) + (ov.spaces.private ?? 0) + (ov.spaces.public ?? 0))} sub={`${ov.spaces.shared ?? 0} shared`} tone="primary" />
            <Stat icon="link" label="Active links" value={String(ov.shares)} sub="public share links" tone="tertiary" />
          </div>
          <h2 className="sec">GitHub repositories</h2>
          <div className="repo-list">
            {ov.repos.length === 0 && <p className="muted">No repositories yet. One is created on the first upload.</p>}
            {ov.repos.map((r) => (
              <div key={r.name} className="repo"><Icon name="inventory_2" /><b>{r.name}</b><div className="meter"><i style={{ width: `${Math.min(100, (r.bytes / r.limit) * 100)}%` }} /></div><span>{fmtBytes(r.bytes)} / {fmtBytes(r.limit)}</span></div>
            ))}
          </div>
        </>
      ))}

      {tab === "users" && (
        <>
          <div className="search tight"><Icon name="search" /><input placeholder="Filter users" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter users" /></div>
          {!users ? <Loader /> : (
            <div className="table-wrap">
              <table className="dtable">
                <thead><tr><th>User</th><th>Storage</th><th>Files</th><th>Spaces</th><th>Last seen</th><th>Joined</th><th /></tr></thead>
                <tbody>
                  {list.map((u) => (
                    <tr key={u.username} className={u.disabled ? "off" : ""}>
                      <td><div className="u-cell"><Avatar name={u.username} /><div><b>@{u.username}</b>{u.is_admin && <span className="badge">admin</span>}{u.disabled && <span className="badge bad">disabled</span>}<small>{u.email}</small></div></div></td>
                      <td><div className="bar-cell"><span>{fmtBytes(u.bytes)}{u.quota_bytes ? ` / ${fmtBytes(u.quota_bytes)}` : ""}</span><div className="meter"><i style={{ width: `${(u.bytes / maxBytes) * 100}%` }} /></div></div></td>
                      <td>{u.files.toLocaleString()}</td>
                      <td>{u.spaces_owned} owned · {u.spaces_joined} joined</td>
                      <td>{fmtAgo(u.last_login_at)}</td><td>{fmtDate(u.created_at)}</td>
                      <td><button className="icon-btn" aria-label={`Actions for ${u.username}`} onClick={(e) => userMenu(e, u)}><Icon name="more_vert" /></button></td>
                    </tr>
                  ))}
                  {list.length === 0 && <tr><td colSpan={7} className="muted center-text pad">No users match</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {tab === "spaces" && (!spaces ? <Loader /> : (
        <div className="table-wrap">
          <table className="dtable">
            <thead><tr><th>Space</th><th>Type</th><th>Owner</th><th>Members</th><th>Files</th><th>Size</th></tr></thead>
            <tbody>
              {spaces.map((s) => (
                <tr key={s.id}><td><b>{s.type === "private" ? `My Drive (@${s.owner})` : s.name}</b></td><td><span className={`badge t-${s.type}`}>{s.type}</span></td><td>{s.owner ? `@${s.owner}` : "everyone"}</td><td>{s.type === "shared" ? s.members + 1 : "-"}</td><td>{s.files.toLocaleString()}</td><td>{fmtBytes(s.bytes)}</td></tr>
              ))}
            </tbody>
          </table>
          <p className="muted small-text pad">Admins see sizes and counts only, never file names or contents.</p>
        </div>
      ))}

      {menu && <Menu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
      {dlg?.k === "quota" && (
        <Dialog title={`Storage quota for @${dlg.u.username}`} onClose={() => setDlg(null)} actions={<>
          <button className="btn text" onClick={() => setDlg(null)}>Cancel</button>
          <button className="btn tonal" onClick={() => { act(() => api.admin.patchUser(dlg.u.username, { clear_quota: true }), "Quota reset to default"); setDlg(null); }}>Use default</button>
          <button className="btn filled" onClick={() => { const n = Number(gb); if (n >= 0) { act(() => api.admin.patchUser(dlg.u.username, { quota_bytes: Math.round(n * 1024 ** 3) }), "Quota saved"); setDlg(null); } }}>Save</button>
        </>}>
          <Field label="Quota in GB (0 = unlimited)" value={gb} onChange={setGb} autoFocus />
        </Dialog>
      )}
      {dlg?.k === "link" && (
        <Dialog title="Password reset link" onClose={() => setDlg(null)} actions={<><button className="btn text" onClick={() => setDlg(null)}>Close</button><button className="btn filled" onClick={async () => toast((await copyText(dlg.link!)) ? "Link copied" : "Copy failed", "success")}><Icon name="content_copy" /> Copy</button></>}>
          <p className="hint">Send this to @{dlg.u.username} yourself. It works once and expires in 30 minutes.</p>
          <input className="plain-input" readOnly value={dlg.link} onFocus={(e) => e.target.select()} aria-label="Reset link" />
        </Dialog>
      )}
      {dlg?.k === "delete" && <ConfirmDialog danger title={`Delete @${dlg.u.username}?`} text="Their account, files, spaces they own and share links are deleted permanently." cta="Delete user" onClose={() => setDlg(null)} onConfirm={async () => { await act(() => api.admin.deleteUser(dlg.u.username), "User deleted"); }} />}
    </div>
  );
}

function Stat({ icon, label, value, sub, tone }: { icon: string; label: string; value: string; sub: string; tone: string }) {
  return <div className={`stat-card ${tone}`}><Icon name={icon} fill /><div><b>{value}</b><span>{label}</span><small>{sub}</small></div></div>;
}
