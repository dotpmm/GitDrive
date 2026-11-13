import { useEffect, useState } from "react";
import { Node, api, fmtBytes, kindOf, triggerDownload } from "../api";
import { Icon, Loader, useToast } from "./ui";

export default function Preview({ nodes, index, onIndex, onClose, onShare, canShare }: {
  nodes: Node[]; index: number; onIndex: (i: number) => void; onClose: () => void; onShare: (n: Node) => void; canShare: (n: Node) => boolean;
}) {
  const node = nodes[index];
  const toast = useToast();
  const kind = kindOf(node);
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setUrl(null); setText(null);
    api.fileUrl(node.id, false).then(async (r) => {
      if (cancelled) return;
      setUrl(r.url);
      if (kind === "text" || kind === "code") {
        try {
          const res = await fetch(r.url, { headers: { Range: "bytes=0-524287" } });
          const t = await res.text();
          if (!cancelled) setText(node.size > 524288 ? t + "\n\n... (preview truncated, download for the full file)" : t);
        } catch { if (!cancelled) setText("Couldn't load a preview."); }
      }
    }).catch((e) => toast(e.message, "error"));
    return () => { cancelled = true; };
  }, [node.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowRight" && index < nodes.length - 1) onIndex(index + 1);
      if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [index, nodes.length, onClose, onIndex]);

  const download = async () => { try { triggerDownload((await api.fileUrl(node.id, true)).url); } catch (e) { toast((e as Error).message, "error"); } };

  return (
    <div className="preview" role="dialog" aria-modal="true" aria-label={`Preview ${node.name}`}>
      <header className="preview-bar">
        <button className="icon-btn" onClick={onClose} aria-label="Close"><Icon name="close" /></button>
        <div className="meta"><div className="name">{node.name}</div><div className="sub">{fmtBytes(node.size)}</div></div>
        {canShare(node) && <button className="icon-btn" onClick={() => onShare(node)} aria-label="Share"><Icon name="share" /></button>}
        <button className="btn filled" onClick={download}><Icon name="download" /> Download</button>
      </header>
      <div className="preview-stage" onClick={(e) => e.target === e.currentTarget && onClose()}>
        {index > 0 && <button className="nav-btn left" onClick={() => onIndex(index - 1)} aria-label="Previous"><Icon name="chevron_left" /></button>}
        {index < nodes.length - 1 && <button className="nav-btn right" onClick={() => onIndex(index + 1)} aria-label="Next"><Icon name="chevron_right" /></button>}
        {!url ? <Loader /> : kind === "image" ? <img src={url} alt={node.name} />
          : kind === "video" ? <video src={url} controls autoPlay playsInline />
          : kind === "audio" ? <div className="audio-card"><span className="file-ico big"><Icon name="music_note" fill /></span><b>{node.name}</b><audio src={url} controls autoPlay /></div>
          : kind === "pdf" ? <iframe src={url} title={node.name} />
          : kind === "text" || kind === "code" ? <pre className="text-preview">{text ?? "Loading…"}</pre>
          : <div className="audio-card"><span className="file-ico big"><Icon name="draft" /></span><b>No preview for this file type</b><button className="btn filled" onClick={download}><Icon name="download" /> Download</button></div>}
      </div>
    </div>
  );
}
