import { DragEvent, MouseEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Invite, Node, Space, api, fmtBytes, fmtDate, triggerDownload } from "../api";
import { useAuth } from "../auth";
import { Avatar, Icon, Loader, Menu, MenuItem, ThemeButton, useToast } from "../components/ui";
import { ConfirmDialog, MembersDialog, MoveDialog, NameDialog, PasswordDialog, ShareDialog, Thumb } from "../components/dialogs";
import Preview from "../components/Preview";
import UploadTray from "../components/UploadTray";
import { Entry, entriesFromDrop, entriesFromInput, uploads } from "../upload";

type View =
  | { t: "space"; space: string; folder: string | null }
  | { t: "recent" } | { t: "starred" } | { t: "trash" } | { t: "search"; q: string };

function parseView(path: string, search: string, me: string): View {
  const seg = path.replace(/^\/app\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (seg[0] === "s" && seg[1]) return { t: "space", space: seg[1], folder: seg[2] === "f" ? seg[3] ?? null : null };
  if (seg[0] === "recent") return { t: "recent" };
  if (seg[0] === "starred") return { t: "starred" };
  if (seg[0] === "trash") return { t: "trash" };
  if (seg[0] === "search") return { t: "search", q: new URLSearchParams(search).get("q") ?? "" };
  return { t: "space", space: `private:${me}`, folder: null };
}

const spaceHref = (space: string, folder?: string | null) => `/app/s/${encodeURIComponent(space)}${folder ? `/f/${folder}` : ""}`;
type SortKey = "name" | "updated" | "size";
type Dlg =
  | { k: "newFolder" } | { k: "rename"; node: Node } | { k: "move"; nodes: Node[] } | { k: "share"; node: Node }
  | { k: "members" } | { k: "newSpace" } | { k: "renameSpace" } | { k: "deleteSpace" } | { k: "password" }
  | { k: "forever"; nodes: Node[] } | { k: "emptyTrash" };

export default function Dashboard() {
  const { user, signOut, refresh } = useAuth();
  const toast = useToast();
  const nav = useNavigate();
  const loc = useLocation();
  const me = user!.username;
  const view = useMemo(() => parseView(loc.pathname, loc.search, me), [loc.pathname, loc.search, me]);

  const [spaces, setSpaces] = useState<Space[]>([]);
  const [nodes, setNodes] = useState<Node[]>([]);
  const [path, setPath] = useState<{ id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [layout, setLayout] = useState<"grid" | "list">(() => (localStorage.getItem("gitdrive.layout") as "grid" | "list") || "grid");
  const [sort, setSort] = useState<{ key: SortKey; asc: boolean }>({ key: "name", asc: true });
  const [dlg, setDlg] = useState<Dlg | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);
  const [preview, setPreview] = useState<number | null>(null);
  const [drawer, setDrawer] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [pop, setPop] = useState<"invites" | "account" | null>(null);
  const [q, setQ] = useState(view.t === "search" ? view.q : "");
  const fileInput = useRef<HTMLInputElement>(null);
  const dirInput = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const lastClick = useRef<string | null>(null);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const spaceById = useMemo(() => Object.fromEntries(spaces.map((s) => [s.id, s])), [spaces]);
  const curSpace = view.t === "space" ? spaceById[view.space] : undefined;
  const viewKey = JSON.stringify(view);

  // ------------------------------------------------------------- loading
  const loadSpaces = useCallback(() => api.spaces().then(setSpaces).catch((e) => toast(e.message, "error")), [toast]);
  useEffect(() => { loadSpaces(); }, [loadSpaces]);

  const loadInvites = useCallback(() => api.invites().then(setInvites).catch(() => {}), []);
  useEffect(() => {
    loadInvites();
    const t = window.setInterval(loadInvites, 20000);
    const f = () => document.visibilityState === "visible" && loadInvites();
    document.addEventListener("visibilitychange", f);
    return () => { window.clearInterval(t); document.removeEventListener("visibilitychange", f); };
  }, [loadInvites]);

  useEffect(() => {
    if (!spaces.length) return;
    let cancelled = false;
    setLoading(true); setError("");
    (async () => {
      try {
        if (view.t === "space") {
          const r = await api.list(view.space, view.folder);
          if (!cancelled) { setNodes(r.nodes); setPath(r.path); }
        } else if (view.t === "recent") { const r = await api.recent(); if (!cancelled) { setNodes(r); setPath([]); } }
        else if (view.t === "starred") { const r = await api.starred(); if (!cancelled) { setNodes(r); setPath([]); } }
        else if (view.t === "search") { const r = view.q.trim() ? await api.search(view.q.trim()) : []; if (!cancelled) { setNodes(r); setPath([]); } }
        else {
          const lists = await Promise.all(spaces.filter((s) => s.role !== "viewer").map((s) => api.list(s.id, null, true).then((r) => r.nodes)));
          if (!cancelled) { setNodes(lists.flat().sort((a, b) => (b.trashed_at ?? "").localeCompare(a.trashed_at ?? ""))); setPath([]); }
        }
        if (!cancelled) setSel(new Set());
      } catch (e) {
        if (!cancelled) { setError((e as Error).message); setNodes([]); }
      } finally { if (!cancelled) setLoading(false); }
    })();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey, tick, spaces.length]);

  useEffect(() => uploads.onDone((j) => {
    refresh();
    if (view.t === "space" && j.space === view.space) reload();
  }), [view, reload, refresh]);

  useEffect(() => { setDrawer(false); setPreview(null); setPop(null); }, [viewKey]);
  useEffect(() => { if (view.t === "search") setQ(view.q); }, [view]);
  useEffect(() => { try { localStorage.setItem("gitdrive.layout", layout); } catch { /* ignore */ } }, [layout]);

  // ---------------------------------------------------------- permissions
  const canEdit = useCallback((n: Node) => {
    const sp = spaceById[n.space];
    if (!sp || sp.role === "viewer") return false;
    return sp.type !== "public" || n.owner === me || user!.is_admin;
  }, [spaceById, me, user]);
  const canUpload = view.t === "space" && !!curSpace && curSpace.role !== "viewer";

  const shown = useMemo(() => {
    const arr = [...nodes];
    const dir = sort.asc ? 1 : -1;
    arr.sort((a, b) => {
      if (view.t === "space" && a.type !== b.type) return a.type === "folder" ? -1 : 1;
      const c = sort.key === "name" ? a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" })
        : sort.key === "size" ? a.size - b.size : (a.updated_at ?? "").localeCompare(b.updated_at ?? "");
      return c * dir;
    });
    return arr;
  }, [nodes, sort, view.t]);
  const selected = useMemo(() => shown.filter((n) => sel.has(n.id)), [shown, sel]);
  const previewable = useMemo(() => shown.filter((n) => n.type === "file"), [shown]);

  // -------------------------------------------------------------- actions
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    try { await fn(); if (ok) toast(ok, "success"); reload(); } catch (e) { toast((e as Error).message, "error"); }
  };
  const open = (n: Node) => {
    if (n.trashed_at) return toast("Restore this item to open it");
    if (n.type === "folder") nav(spaceHref(n.space, n.id));
    else setPreview(previewable.findIndex((p) => p.id === n.id));
  };
  const download = async (list: Node[]) => {
    try {
      if (list.length === 1 && list[0].type === "file") triggerDownload((await api.fileUrl(list[0].id, true)).url);
      else triggerDownload((await api.zipUrl(list[0].space, list.map((n) => n.id), list.length === 1 ? list[0].name : curSpace?.name ?? "GitDrive")).url);
    } catch (e) { toast((e as Error).message, "error"); }
  };
  const extract = async (n: Node) => {
    try {
      const { job_id } = await api.extract(n.id);
      toast(`Extracting ${n.name}…`);
      for (;;) {
        await new Promise((r) => setTimeout(r, 1000));
        const j = await api.job(job_id);
        if (j.status === "done") { toast(`Extracted${j.skipped ? ` (${j.skipped} unsafe entries skipped)` : ""}`, "success"); reload(); refresh(); return; }
        if (j.status === "error") throw new Error(j.error ?? "Extraction failed");
      }
    } catch (e) { toast((e as Error).message, "error"); }
  };
  const startUpload = (entries: Entry[]) => {
    if (!entries.length) return;
    if (!canUpload || view.t !== "space" || !curSpace) return toast("Open one of your spaces to upload here", "error");
    uploads.add(entries, curSpace.id, curSpace.name, view.folder);
  };

  const itemsFor = (n: Node): MenuItem[] => {
    const many = sel.has(n.id) && sel.size > 1 ? selected : [n];
    const edit = many.every(canEdit);
    if (n.trashed_at) return [
      { label: "Restore", icon: "restore_from_trash", onClick: () => run(async () => { await Promise.all(many.map((x) => api.restore(x.id))); }, "Restored"), disabled: !edit },
      { label: "Delete forever", icon: "delete_forever", danger: true, onClick: () => setDlg({ k: "forever", nodes: many }), disabled: !edit },
    ];
    const list: MenuItem[] = [];
    if (many.length === 1) list.push({ label: n.type === "folder" ? "Open" : "Preview", icon: n.type === "folder" ? "folder_open" : "visibility", onClick: () => open(n) });
    list.push({ label: many.length > 1 || n.type === "folder" ? "Download as zip" : "Download", icon: "download", onClick: () => download(many) });
    if (many.length === 1 && edit) list.push({ label: n.shared ? "Manage link" : "Share link", icon: "link", onClick: () => setDlg({ k: "share", node: n }) });
    if (many.length === 1 && n.type === "file" && n.name.toLowerCase().endsWith(".zip") && edit) list.push({ label: "Extract here", icon: "unarchive", onClick: () => extract(n) });
    list.push({ divider: true, label: "" });
    list.push({ label: n.starred ? "Remove star" : "Add star", icon: n.starred ? "star" : "star_border", onClick: () => run(async () => { await Promise.all(many.map((x) => api.patchNode(x.id, { starred: !n.starred }))); }) });
    if (many.length === 1) list.push({ label: "Rename", icon: "edit", onClick: () => setDlg({ k: "rename", node: n }), disabled: !edit });
    if (view.t === "space") list.push({ label: "Move to…", icon: "drive_file_move", onClick: () => setDlg({ k: "move", nodes: many }), disabled: !edit });
    list.push({ divider: true, label: "" });
    list.push({ label: "Move to trash", icon: "delete", danger: true, onClick: () => run(async () => { await Promise.all(many.map((x) => api.trash(x.id))); }, many.length > 1 ? `${many.length} items moved to trash` : "Moved to trash"), disabled: !edit });
    return list;
  };

  const showMenu = (e: MouseEvent, n: Node) => {
    e.preventDefault(); e.stopPropagation();
    if (!sel.has(n.id)) setSel(new Set([n.id]));
    setMenu({ x: e.clientX, y: e.clientY, items: itemsFor(n) });
  };
  const kebab = (e: MouseEvent, n: Node) => {
    e.stopPropagation();
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    if (!sel.has(n.id)) setSel(new Set([n.id]));
    setMenu({ x: r.right - 220, y: r.bottom, items: itemsFor(n) });
  };

  const clickNode = (e: MouseEvent, n: Node) => {
    const coarse = window.matchMedia("(pointer: coarse)").matches;
    if (e.shiftKey && lastClick.current) {
      const a = shown.findIndex((x) => x.id === lastClick.current), b = shown.findIndex((x) => x.id === n.id);
      setSel(new Set(shown.slice(Math.min(a, b), Math.max(a, b) + 1).map((x) => x.id)));
    } else if (e.ctrlKey || e.metaKey || (sel.size > 0 && coarse)) {
      const s = new Set(sel); s.has(n.id) ? s.delete(n.id) : s.add(n.id); setSel(s);
    } else if (coarse) open(n);
    else setSel(new Set([n.id]));
    lastClick.current = n.id;
  };
  const toggleCheck = (e: MouseEvent, n: Node) => {
    e.stopPropagation();
    const s = new Set(sel); s.has(n.id) ? s.delete(n.id) : s.add(n.id); setSel(s); lastClick.current = n.id;
  };

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || dlg || preview !== null) return;
      if (e.key === "Escape") setSel(new Set());
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") { e.preventDefault(); setSel(new Set(shown.map((n) => n.id))); }
      else if ((e.key === "Delete" || e.key === "Backspace") && selected.length && view.t !== "trash" && selected.every(canEdit)) {
        run(async () => { await Promise.all(selected.map((x) => api.trash(x.id))); }, "Moved to trash");
      } else if (e.key === "Enter" && selected.length === 1) open(selected[0]);
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  });

  // ------------------------------------------------------- drag and drop
  const onDragEnter = (e: DragEvent) => { if (e.dataTransfer.types.includes("Files")) { dragDepth.current++; setDragging(true); } };
  const onDragLeave = () => { dragDepth.current = Math.max(0, dragDepth.current - 1); if (!dragDepth.current) setDragging(false); };
  const onDrop = async (e: DragEvent) => {
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault(); dragDepth.current = 0; setDragging(false);
    startUpload(await entriesFromDrop(e.dataTransfer));
  };
  const dropOnFolder = (e: DragEvent, folder: Node) => {
    const raw = e.dataTransfer.getData("application/x-gitdrive");
    if (!raw) return;
    e.preventDefault(); e.stopPropagation();
    const ids: string[] = JSON.parse(raw).filter((id: string) => id !== folder.id);
    if (ids.length) run(async () => { await Promise.all(ids.map((id) => api.patchNode(id, { parent: folder.id }))); }, `Moved into ${folder.name}`);
  };

  // --------------------------------------------------------------- render
  const title = view.t === "space" ? (path.length ? path[path.length - 1].name : curSpace?.name ?? "") : view.t === "recent" ? "Recent" : view.t === "starred" ? "Starred" : view.t === "trash" ? "Trash" : `Results for "${view.q}"`;
  const privateId = `private:${me}`;
  const quota = user!.quota_bytes;

  const submitSearch = () => { if (q.trim()) nav(`/app/search?q=${encodeURIComponent(q.trim())}`); };

  const renderNode = (n: Node) => {
    const isSel = sel.has(n.id);
    const isFolder = n.type === "folder";
    const sp = spaceById[n.space];
    return (
      <div
        key={n.id} className={`item ${layout} ${isSel ? "sel" : ""}`} role="button" tabIndex={0} aria-selected={isSel}
        onClick={(e) => clickNode(e, n)} onDoubleClick={() => open(n)} onContextMenu={(e) => showMenu(e, n)}
        onKeyDown={(e) => e.key === "Enter" && open(n)}
        draggable={view.t === "space" && canEdit(n)}
        onDragStart={(e) => { const ids = sel.has(n.id) ? [...sel] : [n.id]; e.dataTransfer.setData("application/x-gitdrive", JSON.stringify(ids)); e.dataTransfer.effectAllowed = "move"; }}
        onDragOver={(e) => { if (isFolder && e.dataTransfer.types.includes("application/x-gitdrive")) e.preventDefault(); }}
        onDrop={(e) => isFolder && dropOnFolder(e, n)}
      >
        <button className="check" onClick={(e) => toggleCheck(e, n)} aria-label={isSel ? "Deselect" : "Select"}><Icon name={isSel ? "check_circle" : "radio_button_unchecked"} fill={isSel} /></button>
        <Thumb node={n} />
        <div className="meta">
          <div className="name" title={n.name}>{n.name}</div>
          <div className="sub">
            {isFolder ? "Folder" : fmtBytes(n.size)} · {fmtDate(n.updated_at)}
            {view.t !== "space" && sp && <span className="badge">{sp.type === "private" ? "My Drive" : sp.name}</span>}
          </div>
        </div>
        <div className="flags">
          {n.starred && <Icon name="star" fill className="star" />}
          {n.shared && <Icon name="link" className="linkd" />}
          {sp?.type === "public" && n.owner !== me && <span className="badge" title="Uploaded by">@{n.owner}</span>}
        </div>
        <button className="icon-btn kebab" aria-label={`More actions for ${n.name}`} onClick={(e) => kebab(e, n)}><Icon name="more_vert" /></button>
      </div>
    );
  };

  const navItem = (href: string, icon: string, label: string, active: boolean, extra?: string) => (
    <Link key={href} to={href} className={`nav-item ${active ? "active" : ""}`} aria-current={active ? "page" : undefined}>
      <Icon name={icon} fill={active} /><span>{label}</span>{extra && <small>{extra}</small>}
    </Link>
  );

  return (
    <div className="shell" onDragEnter={onDragEnter} onDragLeave={onDragLeave} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      {drawer && <div className="drawer-scrim" onClick={() => setDrawer(false)} />}
      <aside className={`drawer ${drawer ? "open" : ""}`}>
        <Link to="/" className="brand"><span className="brand-mark"><Icon name="cloud" fill /></span><b>GitDrive</b></Link>
        <button className="fab-ext" onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setMenu({ x: r.left, y: r.bottom + 6, items: newItems() }); }}>
          <Icon name="add" /> New
        </button>
        <nav>
          {navItem(spaceHref(privateId), "hard_drive", "My Drive", view.t === "space" && view.space === privateId)}
          {navItem(spaceHref("public"), "public", "Public space", view.t === "space" && view.space === "public")}
          <div className="nav-head"><span>Shared spaces</span><button className="icon-btn" aria-label="New shared space" onClick={() => setDlg({ k: "newSpace" })}><Icon name="add" /></button></div>
          {spaces.filter((s) => s.type === "shared").map((s) => navItem(spaceHref(s.id), "groups", s.name, view.t === "space" && view.space === s.id, s.role === "owner" ? "" : s.role === "editor" ? "edit" : "view"))}
          {!spaces.some((s) => s.type === "shared") && <p className="nav-empty">Create a space to share a folder with friends.</p>}
          <div className="nav-sep" />
          {navItem("/app/recent", "schedule", "Recent", view.t === "recent")}
          {navItem("/app/starred", "star", "Starred", view.t === "starred")}
          {navItem("/app/trash", "delete", "Trash", view.t === "trash")}
        </nav>
        <div className="usage">
          <div className="usage-row"><b>{fmtBytes(user!.used_bytes)}</b><span>{quota ? `of ${fmtBytes(quota)}` : "used"}</span></div>
          {quota > 0 && <div className="meter"><i style={{ width: `${Math.min(100, (user!.used_bytes / quota) * 100)}%` }} /></div>}
        </div>
      </aside>

      <div className="main">
        <header className="appbar">
          <button className="icon-btn menu-btn" onClick={() => setDrawer(true)} aria-label="Open menu"><Icon name="menu" /></button>
          <div className="search">
            <Icon name="search" />
            <input value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submitSearch()} placeholder="Search in GitDrive" aria-label="Search" />
            {q && <button className="icon-btn" aria-label="Clear search" onClick={() => { setQ(""); if (view.t === "search") nav(-1); }}><Icon name="close" /></button>}
          </div>
          <ThemeButton />
          <div className="pop-wrap">
            <button className="icon-btn" onClick={() => setPop(pop === "invites" ? null : "invites")} aria-label={`Invitations (${invites.length})`}>
              <Icon name={invites.length ? "notifications_active" : "notifications"} fill={!!invites.length} />
              {invites.length > 0 && <span className="notif-badge">{invites.length}</span>}
            </button>
            {pop === "invites" && (
              <div className="popover wide-pop">
                <h3>Invitations</h3>
                {invites.length === 0 ? <p className="muted pad">No pending invitations</p> : invites.map((i) => (
                  <div key={i.id} className="inv">
                    <Avatar name={i.from} />
                    <div className="meta"><div className="name"><b>@{i.from}</b> invited you to <b>{i.space_name}</b></div><div className="sub">as {i.role === "editor" ? "editor" : "viewer"}</div></div>
                    <div className="inv-actions">
                      <button className="btn filled sm" onClick={async () => { try { await api.acceptInvite(i.id); toast(`Joined ${i.space_name}`, "success"); await loadSpaces(); loadInvites(); nav(spaceHref(i.space)); } catch (e) { toast((e as Error).message, "error"); } }}>Accept</button>
                      <button className="btn text sm" onClick={async () => { await api.declineInvite(i.id).catch(() => {}); loadInvites(); }}>Decline</button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="pop-wrap">
            <button className="avatar-btn" onClick={() => setPop(pop === "account" ? null : "account")} aria-label="Account"><Avatar name={me} /></button>
            {pop === "account" && (
              <div className="popover">
                <div className="acct"><Avatar name={me} size={56} /><div><b>@{me}</b><small>{user!.email}</small></div></div>
                <div className="usage inline"><div className="usage-row"><b>{fmtBytes(user!.used_bytes)}</b><span>{quota ? `of ${fmtBytes(quota)}` : "used"}</span></div>{quota > 0 && <div className="meter"><i style={{ width: `${Math.min(100, (user!.used_bytes / quota) * 100)}%` }} /></div>}</div>
                {user!.is_admin && <Link className="pop-item" to="/admin"><Icon name="admin_panel_settings" /> Admin panel</Link>}
                <button className="pop-item" onClick={() => { setPop(null); setDlg({ k: "password" }); }}><Icon name="key" /> Change password</button>
                <button className="pop-item" onClick={() => { signOut(); nav("/", { replace: true }); }}><Icon name="logout" /> Sign out</button>
              </div>
            )}
          </div>
        </header>
        {pop && <div className="pop-scrim" onClick={() => setPop(null)} />}

        <main className="content">
          <div className="toolbar">
            <div className="crumbs">
              {view.t === "space" ? (
                <>
                  <Link to={spaceHref(view.space)} className={`crumb ${!path.length ? "cur" : ""}`}>{view.space === privateId ? "My Drive" : curSpace?.name ?? "…"}</Link>
                  {path.map((c, i) => <span key={c.id}><Icon name="chevron_right" /><Link to={spaceHref(view.space, c.id)} className={`crumb ${i === path.length - 1 ? "cur" : ""}`}>{c.name}</Link></span>)}
                </>
              ) : <h1 className="crumb cur">{title}</h1>}
            </div>
            <div className="tools">
              {view.t === "space" && curSpace?.type === "shared" && <button className="btn tonal sm" onClick={() => setDlg({ k: "members" })}><Icon name="group_add" /> {curSpace.member_count} people</button>}
              {view.t === "space" && curSpace?.type === "shared" && curSpace.role === "owner" && (
                <button className="icon-btn" aria-label="Space options" onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setMenu({ x: r.right - 200, y: r.bottom, items: [
                  { label: "Rename space", icon: "edit", onClick: () => setDlg({ k: "renameSpace" }) },
                  { label: "Delete space", icon: "delete_forever", danger: true, onClick: () => setDlg({ k: "deleteSpace" }) },
                ] }); }}><Icon name="more_vert" /></button>
              )}
              {view.t === "trash" && nodes.length > 0 && <button className="btn tonal sm" onClick={() => setDlg({ k: "emptyTrash" })}><Icon name="delete_forever" /> Empty trash</button>}
              <button className="icon-btn" aria-label="Sort" onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); const mk = (key: SortKey, label: string): MenuItem => ({ label: `${label}${sort.key === key ? (sort.asc ? "  ↑" : "  ↓") : ""}`, icon: sort.key === key ? "check" : undefined, onClick: () => setSort((s) => ({ key, asc: s.key === key ? !s.asc : true })) }); setMenu({ x: r.right - 200, y: r.bottom, items: [mk("name", "Name"), mk("updated", "Last modified"), mk("size", "Size")] }); }}><Icon name="sort" /></button>
              <div className="seg" role="group" aria-label="Layout">
                <button className={layout === "list" ? "on" : ""} onClick={() => setLayout("list")} aria-label="List view"><Icon name="view_list" /></button>
                <button className={layout === "grid" ? "on" : ""} onClick={() => setLayout("grid")} aria-label="Grid view"><Icon name="grid_view" /></button>
              </div>
            </div>
          </div>

          {selected.length > 0 && (
            <div className="selbar" role="toolbar" aria-label="Selection">
              <button className="icon-btn" onClick={() => setSel(new Set())} aria-label="Clear selection"><Icon name="close" /></button>
              <b>{selected.length} selected</b><span className="grow" />
              {view.t === "trash" ? <>
                <button className="btn text sm" disabled={!selected.every(canEdit)} onClick={() => run(async () => { await Promise.all(selected.map((x) => api.restore(x.id))); }, "Restored")}><Icon name="restore_from_trash" /> Restore</button>
                <button className="btn text sm danger-t" disabled={!selected.every(canEdit)} onClick={() => setDlg({ k: "forever", nodes: selected })}><Icon name="delete_forever" /> Delete forever</button>
              </> : <>
                <button className="icon-btn" aria-label="Download" onClick={() => download(selected)}><Icon name="download" /></button>
                {selected.length === 1 && selected.every(canEdit) && <button className="icon-btn" aria-label="Share" onClick={() => setDlg({ k: "share", node: selected[0] })}><Icon name="link" /></button>}
                {view.t === "space" && <button className="icon-btn" aria-label="Move" disabled={!selected.every(canEdit)} onClick={() => setDlg({ k: "move", nodes: selected })}><Icon name="drive_file_move" /></button>}
                <button className="icon-btn" aria-label="Move to trash" disabled={!selected.every(canEdit)} onClick={() => run(async () => { await Promise.all(selected.map((x) => api.trash(x.id))); }, "Moved to trash")}><Icon name="delete" /></button>
              </>}
            </div>
          )}

          {loading ? <Loader /> : error ? (
            <div className="empty"><div className="blob err"><Icon name="cloud_off" /></div><h3>Couldn't load this</h3><p>{error}</p><button className="btn filled" onClick={reload}>Try again</button></div>
          ) : shown.length === 0 ? (
            <div className="empty">
              <div className="blob"><Icon name={view.t === "trash" ? "delete" : view.t === "starred" ? "star" : view.t === "search" ? "search_off" : "cloud_upload"} /></div>
              <h3>{view.t === "trash" ? "Trash is empty" : view.t === "starred" ? "Nothing starred yet" : view.t === "recent" ? "No uploads yet" : view.t === "search" ? "No matches" : "Drop files here"}</h3>
              <p>{view.t === "space" && canUpload ? "Drag files or folders anywhere on this page, or use the New button." : view.t === "space" ? "Nothing in here yet." : ""}</p>
              {canUpload && <button className="btn filled xl" onClick={() => fileInput.current?.click()}><Icon name="upload_file" /> Upload files</button>}
            </div>
          ) : (
            <div className={`items ${layout}`} onClick={(e) => e.target === e.currentTarget && setSel(new Set())}>
              {layout === "list" && <div className="list-head"><span /><span /><span>Name</span><span /><span /></div>}
              {shown.map(renderNode)}
            </div>
          )}
        </main>
      </div>

      {canUpload && (
        <div className="fab-wrap">
          <button className="fab" onClick={(e) => { const r = e.currentTarget.getBoundingClientRect(); setMenu({ x: r.right - 230, y: r.top - 190, items: newItems() }); }} aria-label="New"><Icon name="add" /><span className="fab-text">New</span></button>
        </div>
      )}

      <input ref={fileInput} type="file" multiple hidden onChange={(e) => { startUpload(entriesFromInput(e.target.files)); e.target.value = ""; }} />
      <input ref={(el) => { dirInput.current = el; if (el) { el.setAttribute("webkitdirectory", ""); el.setAttribute("directory", ""); } }} type="file" hidden onChange={(e) => { startUpload(entriesFromInput(e.target.files)); e.target.value = ""; }} />

      {dragging && <div className="drop"><div><Icon name="cloud_upload" fill />{canUpload ? `Drop to upload to ${curSpace?.name}` : "You can't upload here"}</div></div>}
      <UploadTray />
      {menu && <Menu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />}
      {preview !== null && previewable[preview] && (
        <Preview nodes={previewable} index={preview} onIndex={setPreview} onClose={() => setPreview(null)} canShare={canEdit} onShare={(n) => { setPreview(null); setDlg({ k: "share", node: n }); }} />
      )}

      {dlg?.k === "newFolder" && view.t === "space" && <NameDialog title="New folder" label="Folder name" initial="Untitled folder" cta="Create" onClose={() => setDlg(null)} onSubmit={async (name) => { await api.createFolder(view.space, name, view.folder); reload(); }} />}
      {dlg?.k === "rename" && <NameDialog title="Rename" label="Name" initial={dlg.node.name} cta="Save" onClose={() => setDlg(null)} onSubmit={async (name) => { await api.patchNode(dlg.node.id, { name }); reload(); }} />}
      {dlg?.k === "newSpace" && <NameDialog title="New shared space" label="Space name" cta="Create" onClose={() => setDlg(null)} onSubmit={async (name) => { const s = await api.createSpace(name); await loadSpaces(); nav(spaceHref(s.id)); }} />}
      {dlg?.k === "renameSpace" && curSpace && <NameDialog title="Rename space" label="Space name" initial={curSpace.name} cta="Save" onClose={() => setDlg(null)} onSubmit={async (name) => { await api.renameSpace(curSpace.id, name); await loadSpaces(); }} />}
      {dlg?.k === "deleteSpace" && curSpace && <ConfirmDialog danger title={`Delete "${curSpace.name}"?`} text="Every file and folder in this space is deleted for everyone, permanently." cta="Delete space" onClose={() => setDlg(null)} onConfirm={async () => { try { await api.deleteSpace(curSpace.id); await loadSpaces(); refresh(); nav(spaceHref(privateId)); toast("Space deleted", "success"); } catch (e) { toast((e as Error).message, "error"); } }} />}
      {dlg?.k === "move" && curSpace && <MoveDialog space={curSpace} nodes={dlg.nodes} onClose={() => setDlg(null)} onMove={async (parent) => { try { await Promise.all(dlg.nodes.map((n) => api.patchNode(n.id, { parent }))); toast("Moved", "success"); reload(); } catch (e) { toast((e as Error).message, "error"); throw e; } }} />}
      {dlg?.k === "share" && <ShareDialog node={dlg.node} onClose={() => setDlg(null)} onChanged={reload} />}
      {dlg?.k === "members" && curSpace && <MembersDialog space={curSpace} onClose={() => setDlg(null)} onChanged={() => { loadSpaces(); }} />}
      {dlg?.k === "password" && <PasswordDialog onClose={() => setDlg(null)} />}
      {dlg?.k === "forever" && <ConfirmDialog danger title="Delete forever?" text={`${dlg.nodes.length === 1 ? `"${dlg.nodes[0].name}"` : `${dlg.nodes.length} items`} will be permanently deleted. This can't be undone.`} cta="Delete forever" onClose={() => setDlg(null)} onConfirm={async () => { try { await Promise.all(dlg.nodes.map((n) => api.deleteForever(n.id))); toast("Deleted", "success"); refresh(); reload(); } catch (e) { toast((e as Error).message, "error"); } }} />}
      {dlg?.k === "emptyTrash" && <ConfirmDialog danger title="Empty trash?" text="Everything in the trash is permanently deleted. This can't be undone." cta="Empty trash" onClose={() => setDlg(null)} onConfirm={async () => { try { await Promise.all(spaces.filter((s) => s.role !== "viewer").map((s) => api.emptyTrash(s.id))); toast("Trash emptied", "success"); refresh(); reload(); } catch (e) { toast((e as Error).message, "error"); } }} />}
    </div>
  );

  function newItems(): MenuItem[] {
    if (!canUpload) return [{ label: "Open one of your spaces to add files", icon: "info", disabled: true }];
    return [
      { label: "New folder", icon: "create_new_folder", onClick: () => setDlg({ k: "newFolder" }) },
      { divider: true, label: "" },
      { label: "File upload", icon: "upload_file", onClick: () => fileInput.current?.click() },
      { label: "Folder upload", icon: "drive_folder_upload", onClick: () => dirInput.current?.click() },
    ];
  }
}

