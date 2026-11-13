import { useEffect, useState } from "react";
import { Crumb, Members, Node, Share, Space, api, fmtBytes, fmtDate, kindOf, KIND_ICON } from "../api";
import { Avatar, Dialog, Field, Icon, Loader, PasswordField, copyText, useToast } from "./ui";
import { useAuth } from "../auth";

// ----------------------------------------------------------- name prompt
export function NameDialog({ title, label, initial = "", cta, onSubmit, onClose }: {
  title: string; label: string; initial?: string; cta: string; onSubmit: (name: string) => Promise<void>; onClose: () => void;
}) {
  const [v, setV] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const go = async () => {
    if (!v.trim()) return setErr("Enter a name");
    setBusy(true); setErr("");
    try { await onSubmit(v.trim()); onClose(); } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  return (
    <Dialog title={title} onClose={onClose} actions={<><button className="btn text" onClick={onClose}>Cancel</button><button className="btn filled" disabled={busy} onClick={go}>{cta}</button></>}>
      <Field label={label} value={v} onChange={setV} autoFocus onEnter={go} maxLength={255} />
      {err && <div className="notice err"><Icon name="error" fill /><span>{err}</span></div>}
    </Dialog>
  );
}

export function ConfirmDialog({ title, text, cta, danger, onConfirm, onClose }: {
  title: string; text: string; cta: string; danger?: boolean; onConfirm: () => Promise<void>; onClose: () => void;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog title={title} onClose={onClose} actions={<>
      <button className="btn text" onClick={onClose}>Cancel</button>
      <button className={`btn ${danger ? "danger" : "filled"}`} disabled={busy} onClick={async () => { setBusy(true); try { await onConfirm(); } finally { onClose(); } }}>{cta}</button>
    </>}>
      <p className="hint">{text}</p>
    </Dialog>
  );
}

// ------------------------------------------------------------ move picker
export function MoveDialog({ space, nodes, onMove, onClose }: { space: Space; nodes: Node[]; onMove: (parent: string) => Promise<void>; onClose: () => void }) {
  const [parent, setParent] = useState<string | null>(null);
  const [path, setPath] = useState<Crumb[]>([]);
  const [folders, setFolders] = useState<Node[] | null>(null);
  const [busy, setBusy] = useState(false);
  const moving = new Set(nodes.map((n) => n.id));
  useEffect(() => {
    setFolders(null);
    api.list(space.id, parent).then((r) => { setFolders(r.nodes.filter((n) => n.type === "folder" && !moving.has(n.id))); setPath(r.path); }).catch(() => setFolders([]));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [space.id, parent]);
  const here = parent ?? "root";
  return (
    <Dialog title={`Move ${nodes.length === 1 ? `"${nodes[0].name}"` : `${nodes.length} items`}`} onClose={onClose} actions={<>
      <button className="btn text" onClick={onClose}>Cancel</button>
      <button className="btn filled" disabled={busy || nodes.every((n) => (n.parent ?? "root") === here)} onClick={async () => { setBusy(true); try { await onMove(here); onClose(); } catch { setBusy(false); } }}>Move here</button>
    </>}>
      <div className="crumbs small">
        <button className="crumb" onClick={() => setParent(null)}>{space.name}</button>
        {path.map((c) => <span key={c.id}><Icon name="chevron_right" /><button className="crumb" onClick={() => setParent(c.id)}>{c.name}</button></span>)}
      </div>
      <div className="picker">
        {folders === null ? <Loader /> : folders.length === 0 ? <p className="muted center-text">No folders here</p> : folders.map((f) => (
          <button key={f.id} className="picker-row" onClick={() => setParent(f.id)}><Icon name="folder" fill /><span>{f.name}</span><Icon name="chevron_right" /></button>
        ))}
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------- sharing
export function ShareDialog({ node, onClose, onChanged }: { node: Node; onClose: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [share, setShare] = useState<Share | null | undefined>(undefined);
  const [days, setDays] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.getShare(node.id).then((r) => setShare(r.share)).catch((e) => { toast(e.message, "error"); onClose(); }); }, [node.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const create = async () => {
    setBusy(true);
    try { setShare((await api.createShare(node.id, days)).share); onChanged(); } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  };
  const stop = async () => {
    setBusy(true);
    try { await api.revokeShare(node.id); setShare(null); onChanged(); toast("Link turned off"); } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  };
  const copy = async (text: string, what: string) => toast((await copyText(text)) ? `${what} copied` : "Couldn't copy, select it manually", "success");

  return (
    <Dialog title={`Share "${node.name}"`} onClose={onClose} actions={<button className="btn text" onClick={onClose}>Done</button>}>
      {share === undefined ? <Loader /> : share === null ? (
        <>
          <p className="hint">Anyone with the link can view this {node.type}. You can turn it off any time.</p>
          <label className="select-row"><span>Link expires</span>
            <select value={days ?? ""} onChange={(e) => setDays(e.target.value ? Number(e.target.value) : null)}>
              <option value="">Never</option><option value="1">In 1 day</option><option value="7">In 7 days</option><option value="30">In 30 days</option>
            </select>
          </label>
          <button className="btn filled xl block" disabled={busy} onClick={create}><Icon name="link" /> Create link</button>
        </>
      ) : (
        <>
          {share.raw_url && <LinkRow icon="play_circle" title="Embed link" sub="Paste in Discord, Slack or Telegram. Videos and images play inline." url={share.raw_url} onCopy={() => copy(share.raw_url!, "Embed link")} />}
          <LinkRow icon="open_in_new" title="Page link" sub="A friendly page with preview and download." url={share.page_url} onCopy={() => copy(share.page_url, "Page link")} />
          <p className="muted small-text">{share.views} view{share.views === 1 ? "" : "s"} · {share.expires_at ? `expires ${fmtDate(share.expires_at)}` : "never expires"}</p>
          <button className="btn tonal block" disabled={busy} onClick={stop}><Icon name="link_off" /> Turn off link</button>
        </>
      )}
    </Dialog>
  );
}

function LinkRow({ icon, title, sub, url, onCopy }: { icon: string; title: string; sub: string; url: string; onCopy: () => void }) {
  return (
    <div className="link-row">
      <span className="file-ico"><Icon name={icon} /></span>
      <div className="meta"><div className="name">{title}</div><div className="sub">{sub}</div><input readOnly value={url} onFocus={(e) => e.target.select()} aria-label={title} /></div>
      <button className="icon-btn tonal" onClick={onCopy} aria-label={`Copy ${title}`}><Icon name="content_copy" /></button>
    </div>
  );
}

// ---------------------------------------------------------------- members
export function MembersDialog({ space, onClose, onChanged }: { space: Space; onClose: () => void; onChanged: () => void }) {
  const toast = useToast();
  const { user } = useAuth();
  const [m, setM] = useState<Members | null>(null);
  const [who, setWho] = useState("");
  const [role, setRole] = useState("editor");
  const [busy, setBusy] = useState(false);
  const owner = space.role === "owner";
  const load = () => api.members(space.id).then(setM).catch((e) => toast(e.message, "error"));
  useEffect(() => { load(); }, [space.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setBusy(true);
    try { await fn(); if (ok) toast(ok, "success"); await load(); onChanged(); } catch (e) { toast((e as Error).message, "error"); }
    setBusy(false);
  };
  const invite = () => who.trim() && run(async () => { await api.invite(space.id, who.trim(), role); setWho(""); }, "Invite sent");

  return (
    <Dialog title={`People in "${space.name}"`} onClose={onClose} wide actions={<button className="btn text" onClick={onClose}>Done</button>}>
      {owner && (
        <div className="invite-row">
          <Field label="Add by username" value={who} onChange={setWho} onEnter={invite} autoComplete="off" />
          <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role"><option value="editor">Can edit</option><option value="viewer">Can view</option></select>
          <button className="btn filled" disabled={busy || !who.trim()} onClick={invite}>Invite</button>
        </div>
      )}
      {!m ? <Loader /> : (
        <ul className="people">
          <li><Avatar name={m.owner ?? "?"} /><div className="meta"><div className="name">{m.owner}{m.owner === user?.username ? " (you)" : ""}</div><div className="sub">Owner</div></div></li>
          {m.members.map((p) => (
            <li key={p.username}>
              <Avatar name={p.username} />
              <div className="meta"><div className="name">{p.username}{p.username === user?.username ? " (you)" : ""}</div></div>
              {owner ? (
                <>
                  <select value={p.role} disabled={busy} onChange={(e) => run(() => api.setRole(space.id, p.username, e.target.value))}><option value="editor">Can edit</option><option value="viewer">Can view</option></select>
                  <button className="icon-btn" aria-label={`Remove ${p.username}`} onClick={() => run(() => api.removeMember(space.id, p.username), `${p.username} removed`)}><Icon name="person_remove" /></button>
                </>
              ) : <span className="badge">{p.role === "editor" ? "Can edit" : "Can view"}</span>}
            </li>
          ))}
          {m.invites.map((i) => (
            <li key={i.id} className="pending">
              <Avatar name={i.to} /><div className="meta"><div className="name">{i.to}</div><div className="sub">Invite pending · {i.role === "editor" ? "can edit" : "can view"}</div></div>
              <button className="icon-btn" aria-label="Cancel invite" onClick={() => run(() => api.revokeInvite(space.id, i.id))}><Icon name="close" /></button>
            </li>
          ))}
        </ul>
      )}
      {!owner && <button className="btn tonal block" disabled={busy} onClick={() => run(async () => { await api.removeMember(space.id, user!.username); onClose(); }, "You left the space")}><Icon name="logout" /> Leave space</button>}
    </Dialog>
  );
}

// --------------------------------------------------------- change password
export function PasswordDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const { signIn } = useAuth();
  const [cur, setCur] = useState(""); const [n1, setN1] = useState(""); const [n2, setN2] = useState("");
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const go = async () => {
    if (n1.length < 8) return setErr("New password must be at least 8 characters");
    if (n1 !== n2) return setErr("Passwords don't match");
    setBusy(true); setErr("");
    try { const r = await api.changePassword(cur, n1); signIn(r.token, r.user); toast("Password changed", "success"); onClose(); } catch (e) { setErr((e as Error).message); setBusy(false); }
  };
  return (
    <Dialog title="Change password" onClose={onClose} actions={<><button className="btn text" onClick={onClose}>Cancel</button><button className="btn filled" disabled={busy} onClick={go}>Save</button></>}>
      <PasswordField label="Current password" value={cur} onChange={setCur} autoFocus autoComplete="current-password" />
      <PasswordField label="New password" value={n1} onChange={setN1} autoComplete="new-password" />
      <PasswordField label="Confirm new password" value={n2} onChange={setN2} autoComplete="new-password" onEnter={go} />
      {err && <div className="notice err"><Icon name="error" fill /><span>{err}</span></div>}
    </Dialog>
  );
}

// ------------------------------------------------------------ item details
export function Thumb({ node }: { node: { type: string; name: string; mime: string | null } }) {
  const k = kindOf(node);
  return <span className={`file-ico ${k}`}><Icon name={KIND_ICON[k]} fill={k === "folder"} /></span>;
}

export function sizeLabel(n: Node): string { return n.type === "folder" ? "" : fmtBytes(n.size); }
