// Upload manager: parallel files, parallel chunks, retries, folder trees.
import { API, ApiError, Node, api, tokenStore } from "./api";

export type JobStatus = "queued" | "uploading" | "finalizing" | "done" | "error" | "cancelled";
export interface UploadJob {
  id: string; name: string; path: string; size: number; space: string; spaceName: string; sent: number;
  status: JobStatus; error?: string; node?: Node;
}
export interface Entry { file: File; path: string } // path = "folder/sub/name.ext" (relative) or just the file name

const FILE_CONCURRENCY = 2;
const CHUNK_CONCURRENCY = 4;
const RETRIES = 3;

type Listener = () => void;
let jobs: UploadJob[] = [];
const listeners = new Set<Listener>();
const controllers = new Map<string, AbortController>();
const queue: { job: UploadJob; entry: Entry; parent: string | null }[] = [];
const doneHooks = new Set<(job: UploadJob) => void>();
let active = 0;
const folderCache = new Map<string, Promise<string | null>>();

const emit = () => { jobs = [...jobs]; listeners.forEach((l) => l()); };
export const uploads = {
  subscribe: (l: Listener) => { listeners.add(l); return () => { listeners.delete(l); }; },
  snapshot: () => jobs,
  onDone: (fn: (job: UploadJob) => void) => { doneHooks.add(fn); return () => { doneHooks.delete(fn); }; },
  add(entries: Entry[], space: string, spaceName: string, parent: string | null) {
    for (const entry of entries) {
      const job: UploadJob = {
        id: crypto.randomUUID?.() ?? String(Math.random()), name: entry.path.split("/").pop() ?? entry.file.name, path: entry.path,
        size: entry.file.size, space, spaceName, sent: 0, status: "queued",
      };
      jobs.push(job);
      queue.push({ job, entry, parent });
    }
    emit();
    pump();
  },
  cancel(id: string) {
    const j = jobs.find((x) => x.id === id);
    if (!j) return;
    if (j.status === "queued") { j.status = "cancelled"; emit(); }
    controllers.get(id)?.abort();
  },
  clearFinished() { jobs = jobs.filter((j) => j.status === "queued" || j.status === "uploading" || j.status === "finalizing"); emit(); },
  retry(id: string) {
    const j = jobs.find((x) => x.id === id);
    const old = failed.get(id);
    if (!j || !old) return;
    j.status = "queued"; j.error = undefined; j.sent = 0;
    queue.push(old); failed.delete(id); emit(); pump();
  },
};
const failed = new Map<string, { job: UploadJob; entry: Entry; parent: string | null }>();

function pump() {
  while (active < FILE_CONCURRENCY && queue.length) {
    const item = queue.shift()!;
    if (item.job.status === "cancelled") continue;
    active++;
    run(item).finally(() => { active--; pump(); });
  }
}

async function resolveParent(space: string, parent: string | null, path: string): Promise<string | null> {
  const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  if (!dir) return parent;
  const key = `${space}|${parent}|${dir}`;
  if (!folderCache.has(key)) {
    const p = api.ensureFolders(space, dir, parent).then((r) => r.id);
    p.catch(() => folderCache.delete(key));
    folderCache.set(key, p);
  }
  return folderCache.get(key)!;
}

function putChunk(url: string, body: Blob, onBytes: (n: number) => void, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    const t = tokenStore.get();
    if (t) xhr.setRequestHeader("Authorization", `Bearer ${t}`);
    xhr.upload.onprogress = (e) => e.lengthComputable && onBytes(e.loaded);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let msg = `HTTP ${xhr.status}`;
      try { msg = JSON.parse(xhr.responseText).detail ?? msg; } catch { /* ignore */ }
      reject(new ApiError(xhr.status, msg));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error"));
    xhr.onabort = () => reject(new DOMException("Cancelled", "AbortError"));
    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(body);
  });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function run(item: { job: UploadJob; entry: Entry; parent: string | null }) {
  const { job, entry, parent } = item;
  const ctrl = new AbortController();
  controllers.set(job.id, ctrl);
  job.status = "uploading"; emit();
  let uploadId: string | null = null;
  try {
    const pid = await resolveParent(job.space, parent, entry.path);
    const start = await api.createUploadSession({
      space: job.space, parent: pid, name: job.name, size: entry.file.size, mime: entry.file.type || "",
    });
    uploadId = start.upload_id;
    const { chunk_size: cs, total_chunks: total } = start;
    const loaded = new Array<number>(total).fill(0);
    let nextIdx = 0;
    const report = () => { job.sent = loaded.reduce((a, b) => a + b, 0); emit(); };

    const worker = async () => {
      while (nextIdx < total) {
        const i = nextIdx++;
        const blob = entry.file.slice(i * cs, Math.min((i + 1) * cs, entry.file.size));
        for (let attempt = 1; ; attempt++) {
          try {
            await putChunk(`${API}/api/uploads/${uploadId}/chunks/${i}`, blob, (n) => { loaded[i] = n; report(); }, ctrl.signal);
            loaded[i] = blob.size; report();
            break;
          } catch (e) {
            if ((e as Error).name === "AbortError") throw e;
            const retriable = !(e instanceof ApiError) || e.status === 0 || e.status >= 500;
            if (!retriable || attempt >= RETRIES) throw e;
            loaded[i] = 0; report();
            await sleep(800 * attempt);
          }
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(CHUNK_CONCURRENCY, Math.max(total, 1)) }, worker));

    job.status = "finalizing"; emit();
    job.node = await api.completeUpload(uploadId);
    job.status = "done"; job.sent = job.size; emit();
    doneHooks.forEach((h) => h(job));
  } catch (e) {
    if (uploadId) api.abortUpload(uploadId).catch(() => {});
    if ((e as Error).name === "AbortError") job.status = "cancelled";
    else { job.status = "error"; job.error = (e as Error).message; failed.set(job.id, item); }
    emit();
  } finally {
    controllers.delete(job.id);
  }
}

// --------------------------------------------------- drag & drop folder reading
interface FsEntry { isFile: boolean; isDirectory: boolean; name: string; file?: (cb: (f: File) => void, err: (e: unknown) => void) => void; createReader?: () => { readEntries: (cb: (e: FsEntry[]) => void, err: (e: unknown) => void) => void } }

async function walk(entry: FsEntry, prefix: string, out: Entry[]) {
  if (entry.isFile) {
    const file = await new Promise<File>((res, rej) => entry.file!(res, rej));
    out.push({ file, path: prefix + entry.name });
  } else if (entry.isDirectory) {
    const reader = entry.createReader!();
    for (;;) {
      const batch = await new Promise<FsEntry[]>((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      for (const child of batch) await walk(child, `${prefix}${entry.name}/`, out);
    }
  }
}

export async function entriesFromDrop(dt: DataTransfer): Promise<Entry[]> {
  const out: Entry[] = [];
  const items = Array.from(dt.items ?? []);
  const roots = items.map((i) => (i as unknown as { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry?.() ?? null);
  if (roots.some(Boolean)) {
    for (const r of roots) if (r) await walk(r, "", out);
    return out;
  }
  return Array.from(dt.files).map((file) => ({ file, path: file.name }));
}

export function entriesFromInput(files: FileList | null): Entry[] {
  return Array.from(files ?? []).map((file) => ({
    file, path: (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
  }));
}
