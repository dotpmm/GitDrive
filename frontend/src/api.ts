// Thin client for the GitDrive FastAPI backend.

export const CHUNK_SIZE = 10 * 1024 * 1024; // must match what the server expects (10 MB)

export interface Settings {
  apiUrl: string;
  apiKey: string;
}

export interface FileRecord {
  id: string;
  sha256: string;
  original_filename: string;
  relative_path: string;
  repo: string;
  mime_type: string;
  size_bytes: number;
  is_chunked: boolean;
  chunk_count: number;
  uploaded_at: string;
}

const KEY = "gitdrive.settings";

export function loadSettings(): Settings {
  let saved: Partial<Settings> = {};
  try {
    saved = JSON.parse(localStorage.getItem(KEY) ?? "{}");
  } catch {
    /* ignore corrupt storage */
  }
  return {
    apiUrl: saved.apiUrl ?? (import.meta.env.VITE_API_URL as string | undefined) ?? "",
    apiKey: saved.apiKey ?? "",
  };
}

export function saveSettings(s: Settings) {
  try {
    localStorage.setItem(KEY, JSON.stringify(s));
  } catch {
    /* storage unavailable */
  }
}

const base = (s: Settings) => s.apiUrl.replace(/\/+$/, "");
const headers = (s: Settings): HeadersInit => (s.apiKey ? { "X-API-Key": s.apiKey } : {});

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function check(res: Response) {
  if (res.ok) return res;
  let detail = res.statusText;
  try {
    const j = await res.json();
    detail = typeof j.detail === "string" ? j.detail : detail;
  } catch {
    /* not json */
  }
  throw new ApiError(res.status, detail || `HTTP ${res.status}`);
}

export async function listFiles(s: Settings): Promise<FileRecord[]> {
  const res = await check(await fetch(`${base(s)}/files`, { headers: headers(s), cache: "no-store" }));
  return res.json();
}

export async function ping(s: Settings): Promise<boolean> {
  try {
    const res = await fetch(`${base(s)}/health`);
    return res.ok;
  } catch {
    return false;
  }
}

export interface UploadHandle {
  /** resolves with the file sha256 (or "duplicate") */
  done: Promise<{ sha256: string; duplicate: boolean }>;
  cancel: () => void;
}

export interface UploadProgress {
  /** 0-100, bytes sent from the browser to the API */
  sent: number;
  /** 0-100, chunks pushed by the API to GitHub */
  synced: number;
  phase: "sending" | "syncing";
}

function uuid(): string {
  return crypto.randomUUID ? crypto.randomUUID() : Array.from({ length: 4 }, () => Math.random().toString(36).slice(2, 10)).join("-");
}

function sendChunk(url: string, h: HeadersInit, fd: FormData, onBytes: (n: number) => void, signal: AbortSignal) {
  return new Promise<{ status: string; sha256?: string }>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    for (const [k, v] of Object.entries(h)) xhr.setRequestHeader(k, v as string);
    xhr.upload.onprogress = (e) => e.lengthComputable && onBytes(e.loaded);
    xhr.onload = () => {
      let body: { status?: string; sha256?: string; detail?: string } = {};
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve({ status: body.status ?? "", sha256: body.sha256 });
      else reject(new ApiError(xhr.status, body.detail ?? `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error"));
    xhr.onabort = () => reject(new DOMException("Cancelled", "AbortError"));
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(fd);
  });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function uploadFile(
  s: Settings,
  file: File,
  relativePath: string,
  onProgress: (p: UploadProgress) => void,
): UploadHandle {
  const ctrl = new AbortController();
  const uploadId = uuid();

  const run = async () => {
    const total = Math.max(1, Math.ceil(file.size / CHUNK_SIZE));
    let sentBefore = 0;
    let last: { status: string; sha256?: string } = { status: "" };

    for (let i = 0; i < total; i++) {
      const blob = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size));
      const fd = new FormData();
      fd.append("chunk", blob, file.name);
      fd.append("upload_id", uploadId);
      fd.append("chunk_index", String(i));
      fd.append("total_chunks", String(total));
      fd.append("filename", file.name);
      fd.append("mime_type", file.type || "application/octet-stream");
      fd.append("size_bytes", String(file.size));
      fd.append("relative_path", relativePath);

      last = await sendChunk(
        `${base(s)}/upload`,
        headers(s),
        fd,
        (n) => onProgress({ sent: Math.min(99.9, ((sentBefore + n) / Math.max(1, file.size)) * 100), synced: 0, phase: "sending" }),
        ctrl.signal,
      );
      sentBefore += blob.size;
    }

    onProgress({ sent: 100, synced: 0, phase: "syncing" });

    // the server keeps the session around until we ask for its final status
    for (;;) {
      if (ctrl.signal.aborted) throw new DOMException("Cancelled", "AbortError");
      const res = await fetch(`${base(s)}/upload/status/${uploadId}`, { headers: headers(s), signal: ctrl.signal });
      if (res.status === 404) {
        if (last.sha256) return { sha256: last.sha256, duplicate: false };
        throw new ApiError(404, "Upload session disappeared");
      }
      await check(res);
      const st = await res.json();
      onProgress({ sent: 100, synced: st.github_percent ?? 0, phase: "syncing" });
      if (st.status === "done") return { sha256: st.sha256 as string, duplicate: false };
      if (st.status === "duplicate") return { sha256: st.sha256 as string, duplicate: true };
      if (st.status === "error") throw new ApiError(500, st.error ?? "Upload failed on the server");
      await sleep(700);
    }
  };

  return { done: run(), cancel: () => ctrl.abort() };
}

/** Download a file through the API (so the API key header can be sent) and return a Blob. */
export async function downloadBlob(
  s: Settings,
  rec: Pick<FileRecord, "sha256" | "mime_type" | "size_bytes">,
  onProgress?: (pct: number) => void,
): Promise<Blob> {
  const res = await check(await fetch(`${base(s)}/download/${rec.sha256}`, { headers: headers(s) }));
  const total = Number(res.headers.get("content-length")) || rec.size_bytes || 0;
  if (!res.body) return res.blob();

  const reader = res.body.getReader();
  const parts: BlobPart[] = [];
  let got = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value as BlobPart);
    got += value.length;
    if (total && onProgress) onProgress(Math.min(100, (got / total) * 100));
  }
  return new Blob(parts, { type: rec.mime_type });
}

export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

export function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}
