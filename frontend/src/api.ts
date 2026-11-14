// Typed client for the GitDrive v4 API.
export const API: string = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/+$/, "") ?? "";

export interface User { username: string; email: string; is_admin: boolean; used_bytes: number; quota_bytes: number; created_at: string }
export interface Space { id: string; name: string; type: "private" | "public" | "shared"; owner: string | null; role: "viewer" | "editor" | "owner"; member_count: number }
export interface Node {
  id: string; space: string; parent: string | null; type: "file" | "folder"; name: string; size: number; mime: string | null;
  owner: string; created_at: string; updated_at: string; trashed_at: string | null; starred: boolean; shared: boolean;
}
export interface Crumb { id: string; name: string }
export interface Listing { nodes: Node[]; path: Crumb[]; role: Space["role"] }
export interface Share { token: string; page_url: string; raw_url: string | null; app_url: string; expires_at: string | null; views: number }
export interface Invite { id: string; space: string; space_name: string; from: string; role: string; created_at: string }
export interface Members { owner: string | null; role: string; members: { username: string; role: string }[]; invites: { id: string; to: string; role: string }[] }
export interface SharedInfo {
  root: { id: string; name: string; type: "file" | "folder"; size: number; mime: string | null; owner: string; raw_url?: string };
  views: number; path: Crumb[];
  children: { id: string; name: string; type: "file" | "folder"; size: number; mime: string | null; raw_url?: string }[];
}
export interface AdminUser {
  username: string; email: string; created_at: string; last_login_at: string | null; disabled: boolean; is_admin: boolean;
  files: number; bytes: number; spaces_owned: number; spaces_joined: number; quota_bytes: number | null;
}
export interface AdminOverview {
  users: number; disabled_users: number; spaces: Record<string, number>; files: number; bytes: number; folders: number; shares: number;
  repos: { name: string; bytes: number; limit: number }[];
}
export interface AdminSpace { id: string; name: string; type: string; owner: string | null; members: number; files: number; bytes: number }

const TOKEN_KEY = "gitdrive.token";
export const tokenStore = {
  get: () => { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } },
  set: (t: string) => { try { localStorage.setItem(TOKEN_KEY, t); } catch { /* private mode */ } },
  clear: () => { try { localStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } },
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

async function parseError(res: Response): Promise<ApiError> {
  let msg = res.statusText || `HTTP ${res.status}`;
  try {
    const j = await res.json();
    if (typeof j.detail === "string") msg = j.detail;
    else if (Array.isArray(j.detail) && j.detail[0]?.msg) msg = String(j.detail[0].msg);
  } catch { /* not json */ }
  return new ApiError(res.status, msg);
}

export async function request<T>(method: string, path: string, body?: unknown, opts: { auth?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = tokenStore.get();
  if (token && opts.auth !== false) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`${API}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Can't reach the server. If it was idle it may be waking up, try again in a few seconds.");
  }
  if (!res.ok) {
    const err = await parseError(res);
    if (res.status === 401 && token && opts.auth !== false) onUnauthorized();
    throw err;
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

const get = <T,>(p: string, o?: { auth?: boolean }) => request<T>("GET", p, undefined, o);
const post = <T,>(p: string, b?: unknown, o?: { auth?: boolean }) => request<T>("POST", p, b ?? {}, o);
const patch = <T,>(p: string, b: unknown) => request<T>("PATCH", p, b);
const del = <T,>(p: string) => request<T>("DELETE", p);
const q = encodeURIComponent;

export const api = {
  signup: (username: string, password: string, email: string) => post<{ token: string; user: User }>("/api/auth/signup", { username, password, email }, { auth: false }),
  login: (username: string, password: string) => post<{ token: string; user: User }>("/api/auth/login", { username, password }, { auth: false }),
  forgot: (email: string) => post<{ ok: boolean }>("/api/auth/forgot", { email }, { auth: false }),
  reset: (token: string, password: string) => post<{ ok: boolean }>("/api/auth/reset", { token, password }, { auth: false }),
  me: () => get<User>("/api/me"),
  changePassword: (current_password: string, new_password: string) => post<{ token: string; user: User }>("/api/me/password", { current_password, new_password }),

  spaces: () => get<Space[]>("/api/spaces"),
  createSpace: (name: string) => post<Space>("/api/spaces", { name }),
  renameSpace: (id: string, name: string) => patch<Space>(`/api/spaces/${q(id)}`, { name }),
  deleteSpace: (id: string) => del(`/api/spaces/${q(id)}`),
  members: (id: string) => get<Members>(`/api/spaces/${q(id)}/members`),
  invite: (id: string, username: string, role: string) => post(`/api/spaces/${q(id)}/invites`, { username, role }),
  revokeInvite: (id: string, inv: string) => del(`/api/spaces/${q(id)}/invites/${inv}`),
  setRole: (id: string, user: string, role: string) => patch(`/api/spaces/${q(id)}/members/${q(user)}`, { role }),
  removeMember: (id: string, user: string) => del(`/api/spaces/${q(id)}/members/${q(user)}`),
  invites: () => get<Invite[]>("/api/invites"),
  acceptInvite: (id: string) => post<{ space: string }>(`/api/invites/${id}/accept`),
  declineInvite: (id: string) => post(`/api/invites/${id}/decline`),

  list: (space: string, parent: string | null, trashed = false) =>
    get<Listing>(`/api/spaces/${q(space)}/nodes?parent=${parent ?? "root"}&trashed=${trashed}`),
  recent: () => get<Node[]>("/api/recent"),
  starred: () => get<Node[]>("/api/starred"),
  search: (term: string) => get<Node[]>(`/api/search?q=${q(term)}`),
  createFolder: (space: string, name: string, parent: string | null) => post<Node>(`/api/spaces/${q(space)}/folders`, { name, parent }),
  ensureFolders: (space: string, path: string, parent: string | null) => post<{ id: string | null }>(`/api/spaces/${q(space)}/folders/ensure`, { path, parent }),
  patchNode: (id: string, body: { name?: string; parent?: string; starred?: boolean }) => patch<Node>(`/api/nodes/${id}`, body),
  trash: (id: string) => post(`/api/nodes/${id}/trash`),
  restore: (id: string) => post(`/api/nodes/${id}/restore`),
  deleteForever: (id: string) => del(`/api/nodes/${id}`),
  emptyTrash: (space: string) => post(`/api/spaces/${q(space)}/trash/empty`),
  getShare: (id: string) => get<{ share: Share | null }>(`/api/nodes/${id}/share`),
  createShare: (id: string, expires_days: number | null) => post<{ share: Share }>(`/api/nodes/${id}/share`, { expires_days }),
  revokeShare: (id: string) => del(`/api/nodes/${id}/share`),
  fileUrl: (id: string, download: boolean) => post<{ url: string }>(`/api/nodes/${id}/url`, { download }),
  zipUrl: (space: string, ids: string[], name: string) => post<{ url: string }>("/api/zip-url", { space, ids, name }),
  createUploadSession: (b: { space: string; parent: string | null; name: string; size: number; mime: string }) =>
    post<{ upload_id: string; chunk_size: number; total_chunks: number }>("/api/uploads", b),
  completeUpload: (id: string) => post<Node>(`/api/uploads/${id}/complete`),
  abortUpload: (id: string) => del(`/api/uploads/${id}`),
  extract: (id: string) => post<{ job_id: string }>(`/api/nodes/${id}/extract`),
  job: (id: string) => get<{ status: string; done: number; total: number; skipped: number; error: string | null; folder?: string }>(`/api/jobs/${id}`),

  shared: (token: string, parent?: string) => get<SharedInfo>(`/api/shared/${q(token)}${parent ? `?parent=${parent}` : ""}`, { auth: false }),

  admin: {
    overview: () => get<AdminOverview>("/api/admin/overview"),
    users: () => get<AdminUser[]>("/api/admin/users"),
    spaces: () => get<AdminSpace[]>("/api/admin/spaces"),
    patchUser: (u: string, body: { disabled?: boolean; quota_bytes?: number; clear_quota?: boolean }) => patch(`/api/admin/users/${q(u)}`, body),
    resetLink: (u: string) => post<{ link: string }>(`/api/admin/users/${q(u)}/reset-link`),
    deleteUser: (u: string) => del(`/api/admin/users/${q(u)}`),
  },
};

// ----------------------------------------------------------------- helpers
export function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }) });
}

export function fmtAgo(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fmtDate(iso);
}

export type Kind = "folder" | "image" | "video" | "audio" | "pdf" | "archive" | "text" | "code" | "doc" | "sheet" | "file";

export function kindOf(n: { type: string; name: string; mime: string | null }): Kind {
  if (n.type === "folder") return "folder";
  const m = n.mime ?? "";
  const ext = n.name.split(".").pop()?.toLowerCase() ?? "";
  if (m.startsWith("image/")) return "image";
  if (m.startsWith("video/")) return "video";
  if (m.startsWith("audio/")) return "audio";
  if (m === "application/pdf" || ext === "pdf") return "pdf";
  if (["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"].includes(ext)) return "archive";
  if (["js", "ts", "tsx", "jsx", "py", "c", "cpp", "h", "java", "go", "rs", "json", "yml", "yaml", "html", "css", "sh", "sql", "xml", "md", "toml"].includes(ext)) return "code";
  if (m.startsWith("text/") || ext === "txt" || ext === "log") return "text";
  if (["doc", "docx", "odt", "ppt", "pptx", "rtf"].includes(ext)) return "doc";
  if (["xls", "xlsx", "csv", "ods"].includes(ext)) return "sheet";
  return "file";
}

export const KIND_ICON: Record<Kind, string> = {
  folder: "folder", image: "image", video: "movie", audio: "music_note", pdf: "picture_as_pdf", archive: "folder_zip",
  text: "article", code: "code", doc: "description", sheet: "table_chart", file: "draft",
};

export function triggerDownload(url: string) {
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
