import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { SharedInfo, api, fmtBytes, kindOf, KIND_ICON, triggerDownload } from "../api";
import { Icon, Loader, ThemeButton, copyText, useToast } from "../components/ui";

type Item = SharedInfo["children"][number];

export default function Share() {
  const { token = "" } = useParams();
  const toast = useToast();
  const [info, setInfo] = useState<SharedInfo | null>(null);
  const [err, setErr] = useState("");
  const [folder, setFolder] = useState<string | undefined>();
  const [open, setOpen] = useState<Item | null>(null);

  useEffect(() => {
    setInfo(null); setErr("");
    api.shared(token, folder).then(setInfo).catch((e) => setErr(e.message));
  }, [token, folder]);

  const root = info?.root;
  const single = root?.type === "file";
  const current: Item | null = single && root ? { id: root.id, name: root.name, type: "file", size: root.size, mime: root.mime, raw_url: root.raw_url } : open;
  const copy = async (text: string) => toast((await copyText(text)) ? "Link copied" : "Couldn't copy", "success");
  const dl = (u: string) => triggerDownload(`${u}${u.includes("?") ? "&" : "?"}dl=1`);

  return (
    <div className="share-page">
      <header className="l-nav">
        <Link to="/" className="brand"><span className="brand-mark"><Icon name="cloud" fill /></span><b>GitDrive</b></Link>
        <nav><ThemeButton /><Link to="/signup" className="btn tonal">Get your own drive</Link></nav>
      </header>
      {err ? (
        <div className="empty"><div className="blob err"><Icon name="link_off" /></div><h3>This link isn't available</h3><p>{err}</p><Link to="/" className="btn filled">Go to GitDrive</Link></div>
      ) : !info || !root ? <Loader /> : (
        <main className="share-card">
          <div className="share-head">
            <span className={`file-ico big ${kindOf(root)}`}><Icon name={KIND_ICON[kindOf(root)]} fill={root.type === "folder"} /></span>
            <div className="meta"><h1>{root.name}</h1><p className="muted">Shared by @{root.owner}{single ? ` · ${fmtBytes(root.size)}` : ""} · {info.views} view{info.views === 1 ? "" : "s"}</p></div>
            {single && root.raw_url && <div className="share-actions"><button className="btn tonal" onClick={() => copy(root.raw_url!)}><Icon name="content_copy" /> Copy link</button><button className="btn filled" onClick={() => dl(root.raw_url!)}><Icon name="download" /> Download</button></div>}
          </div>

          {single && current?.raw_url && <Viewer item={current} />}

          {!single && (
            <>
              <div className="crumbs small">
                <button className="crumb" onClick={() => { setFolder(undefined); setOpen(null); }}>{root.name}</button>
                {info.path.map((c) => <span key={c.id}><Icon name="chevron_right" /><button className="crumb" onClick={() => { setFolder(c.id); setOpen(null); }}>{c.name}</button></span>)}
              </div>
              {open && open.raw_url && <div className="viewer-wrap"><div className="viewer-bar"><b>{open.name}</b><span className="grow" /><button className="btn text sm" onClick={() => dl(open.raw_url!)}><Icon name="download" /> Download</button><button className="icon-btn" aria-label="Close preview" onClick={() => setOpen(null)}><Icon name="close" /></button></div><Viewer item={open} /></div>}
              {info.children.length === 0 ? <p className="muted center-text pad">This folder is empty</p> : (
                <div className="items list">
                  {info.children.map((c) => (
                    <div key={c.id} className="item list" role="button" tabIndex={0} onClick={() => (c.type === "folder" ? (setFolder(c.id), setOpen(null)) : setOpen(c))} onKeyDown={(e) => e.key === "Enter" && (c.type === "folder" ? setFolder(c.id) : setOpen(c))}>
                      <span /><span className={`file-ico ${kindOf(c)}`}><Icon name={KIND_ICON[kindOf(c)]} fill={c.type === "folder"} /></span>
                      <div className="meta"><div className="name">{c.name}</div><div className="sub">{c.type === "folder" ? "Folder" : fmtBytes(c.size)}</div></div>
                      <span />
                      {c.raw_url && <button className="icon-btn kebab show" aria-label={`Download ${c.name}`} onClick={(e) => { e.stopPropagation(); dl(c.raw_url!); }}><Icon name="download" /></button>}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </main>
      )}
    </div>
  );
}

function Viewer({ item }: { item: Item }) {
  const k = kindOf(item);
  const url = item.raw_url!;
  if (k === "video") return <video className="viewer" src={url} controls playsInline preload="metadata" />;
  if (k === "image") return <img className="viewer" src={url} alt={item.name} />;
  if (k === "audio") return <audio className="viewer-audio" src={url} controls />;
  if (k === "pdf") return <iframe className="viewer pdf" src={url} title={item.name} />;
  return <p className="muted center-text pad">No preview for this file type. Use Download.</p>;
}
