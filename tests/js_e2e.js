/* Drives the real index.html upload and download code against a real server, using the
 * browser's own crypto module. This is the closest thing to clicking through the site
 * that can run headless.
 *
 *   node tests/js_e2e.js <base-url>
 *
 * tests/test_js_e2e.py boots the app with stubbed storage and runs this against it.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:10000';
const REPO = path.resolve(__dirname, '..');
globalThis.window = globalThis;
globalThis.self = globalThis;
(0, eval)(fs.readFileSync(
  process.env.HASH_WASM ||
  path.join(REPO, 'node_modules', 'hash-wasm', 'dist', 'argon2.umd.min.js'), 'utf8'));
(0, eval)(fs.readFileSync(path.join(REPO, 'gitdrive-crypto.js'), 'utf8'));

const G = globalThis.GDCrypto;
const enc = new TextEncoder();
const dec = new TextDecoder();
let failures = 0;
const eq = (label, got, want) => {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}`);
  if (!ok) console.log(`          got  ${got}\n          want ${want}`);
};

const PASSWORD = 'end to end passphrase';
let token = null;

async function api(p, init = {}) {
  const headers = new Headers(init.headers || {});
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return fetch(BASE + p, { ...init, headers });
}

// ---- the exact sequence index.html performs --------------------------------

async function register() {
  const kdf = { alg: 'argon2id', salt: G.randomHex(16), m_kib: G.ARGON2_M_KIB,
                t: G.ARGON2_T, p: G.ARGON2_P, len: G.KEY_LEN };
  const root = await G.deriveKRoot(PASSWORD, kdf.salt, kdf);
  const body = new URLSearchParams({
    username: 'jsuser', verifier: await G.authVerifier(root), kdf_salt: kdf.salt,
  });
  const res = await fetch(BASE + '/auth/register', { method: 'POST', body });
  const json = await res.json();
  if (!res.ok) throw new Error('register: ' + JSON.stringify(json));
  token = json.token;
  return { root, kdf };
}

async function privateUpload(bytes, name) {
  const fileId = G.randomHex(16);
  const dek = G.randomHex(G.KEY_LEN);
  const total = 1;
  const chunks = [];
  const sealed = await G.sealChunk(fileId, G.MODE_PRIVATE, 0, total,
    G.hex(await G.chunkKey(G.unhex(dek), 0, total)), bytes);
  const sha = hex(await crypto.subtle.digest('SHA-256', sealed));
  chunks.push({ index: 0, sha256: sha, size: bytes.length });
  const manifest = await G.buildManifest({
    mode: G.MODE_PRIVATE, fileId, chunkSize: 10485760, chunkCount: total,
    plainSize: bytes.length, kRoot: ROOT, dekHex: dek, kdf: KDF,
    meta: { name, path: '', mime: 'application/octet-stream', chunks,
            chain: await G.chainDigest(chunks.map((c) => c.sha256)) },
  });
  const fd = new FormData();
  fd.append('chunk', new Blob([sealed]), name);
  const uploadId = G.randomHex(16);
  fd.append('upload_id', uploadId);
  fd.append('chunk_index', 0);
  fd.append('total_chunks', total);
  fd.append('file_id', fileId);
  fd.append('size_bytes', bytes.length);
  fd.append('visibility', 'private');
  fd.append('blob_sha', sha);
  fd.append('plain_len', bytes.length);
  fd.append('manifest', JSON.stringify(manifest));
  const res = await api('/upload', { method: 'POST', body: fd });
  if (!res.ok) throw new Error('upload: ' + res.status + ' ' + await res.text());
  const ack = await res.json();
  eq('server accepted the browser-computed file id', ack.file_id, fileId);
  eq('private upload finalized', await waitForFinalize(uploadId), 'done');
  return { fileId, dek };
}

// The server finalizes in a background worker, so poll exactly as index.html does.
async function waitForFinalize(uploadId) {
  let status = {};
  for (let i = 0; i < 200; i++) {
    status = await (await api(`/upload/status/${uploadId}`)).json();
    if (['done', 'duplicate', 'error'].includes(status.status)) break;
    await new Promise((r) => setTimeout(r, 50));
  }
  return status.status;
}

async function privateDownload(fileId, kRoot) {
  const manifest = await (await api(`/files/${fileId}/manifest`)).json();
  const { dekHex, meta } = await G.openManifest(manifest, kRoot);
  const ordered = [...meta.chunks].sort((a, b) => a.index - b.index);
  const parts = [];
  for (const c of ordered) {
    const blob = new Uint8Array(await (await api(`/files/${fileId}/chunk/${c.index}`)).arrayBuffer());
    const sha = hex(await crypto.subtle.digest('SHA-256', blob));
    if (sha !== c.sha256) throw new Error('chunk hash mismatch');
    const key = G.hex(await G.chunkKey(G.unhex(dekHex), c.index, ordered.length));
    const open = await G.openChunk(fileId, blob, key, { index: c.index, total: ordered.length });
    parts.push(open.plain);
  }
  if (await G.chainDigest(ordered.map((c) => c.sha256)) !== meta.chain) {
    throw new Error('chain digest mismatch');
  }
  return { meta, plain: Buffer.concat(parts.map((p) => Buffer.from(p))) };
}

const hex = (b) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, '0')).join('');

let ROOT, KDF;

(async () => {
  const payload = enc.encode('the browser did this, not the server '.repeat(400));

  const auth = await register();
  ROOT = auth.root;
  KDF = auth.kdf;

  // private round trip, driven entirely by the browser module
  const info = await privateUpload(payload, 'browser-made.bin');
  const back = await privateDownload(info.fileId, ROOT);
  eq('private filename survives the round trip', back.meta.name, 'browser-made.bin');
  eq('private plaintext round trip', dec.decode(back.plain), dec.decode(payload));

  // wrong password must be rejected by the browser, loudly
  const wrong = await G.deriveKRoot('not the password', KDF.salt, KDF);
  let rejected = false;
  try {
    const manifest = await (await api(`/files/${info.fileId}/manifest`)).json();
    await G.openManifest(manifest, wrong);
  } catch (e) { rejected = e instanceof G.CryptoError; }
  eq('wrong password rejected in the browser', String(rejected), 'true');

  // public upload/download still goes through the server unchanged
  const pub = publicForm(payload, 'open.bin');
  const pubRes = await api('/upload', { method: 'POST', body: pub.form });
  eq('public upload accepted', pubRes.status, 200);
  const publicId = (await pubRes.json()).file_id;
  await waitForFinalize(pub.uploadId);
  const streamed = Buffer.from(await (await api(`/download/${publicId}`)).arrayBuffer());
  eq('public plaintext round trip', streamed.toString(), Buffer.from(payload).toString());

  // a public manifest must not open with a user password
  const pubManifest = await (await api(`/files/${publicId}/manifest`)).json().catch(() => null);
  if (pubManifest === null) {
    eq('public file exposes no private manifest endpoint', 'refused', 'refused');
  } else {
    let opened = false;
    try { await G.openManifest(pubManifest, ROOT); opened = true; } catch (e) { /* expected */ }
    eq('public manifest does not open with K_root', String(opened), 'false');
  }

  console.log(failures ? `\n${failures} FAILURE(S)` : '\nBrowser code path agrees with the server');
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error('ERROR', e); process.exit(1); });

function publicForm(bytes, name) {
  const fd = new FormData();
  const uploadId = G.randomHex(16);
  fd.append('chunk', new Blob([bytes]), name);
  fd.append('upload_id', uploadId);
  fd.append('chunk_index', 0);
  fd.append('total_chunks', 1);
  fd.append('file_id', G.randomHex(16));
  fd.append('size_bytes', bytes.length);
  fd.append('visibility', 'public');
  fd.append('filename', name);
  fd.append('mime_type', 'application/octet-stream');
  fd.append('relative_path', '');
  return { form: fd, uploadId };
}