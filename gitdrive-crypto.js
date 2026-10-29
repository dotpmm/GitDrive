/* GitDrive on-disk crypto format, version 1 -- browser side.
 *
 * This is the client half of api/crypto.py and follows it field for field. Anything
 * changed here must be changed there too; tests/vectors.json pins the two together.
 *
 *   password -> Argon2id -> K_root -> HKDF-SHA256 -> KEK -> { DEK, K_meta }
 *
 * The password and K_root exist only inside this file's callers' memory: they are
 * never placed in localStorage, a cookie, or a request body.
 *
 * Argon2id comes from hash-wasm (MIT, WASM inlined); HKDF-SHA256 and AES-256-GCM come
 * from the platform's Web Crypto API.
 */
window.GDCrypto = (() => {
  'use strict';

  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const utf8 = (s) => enc.encode(s);

  const FORMAT_VERSION = 1;
  const MODE_PRIVATE = 1;
  const MODE_PUBLIC = 2;

  const KEY_LEN = 32;
  const NONCE_LEN = 12;
  const TAG_LEN = 16;

  const CHUNK_MAGIC = [0x47, 0x44, 0x43, 0x31];           // "GDC1"
  const CHUNK_HEADER_LEN = 4 + 1 + 4 + 4 + 4 + NONCE_LEN;  // magic, mode, index, total, len, nonce
  const CHUNK_AAD_PREFIX = utf8('GDC1-AAD/1');

  const ARGON2_M_KIB = 65536;
  const ARGON2_T = 3;
  const ARGON2_P = 1;
  const ARGON2_MIN_M_KIB = 8192;
  const ARGON2_MIN_T = 1;

  const INFO_KEK = utf8('gitdrive/v1/kek');
  const INFO_META = utf8('gitdrive/v1/meta');
  const INFO_AUTH = utf8('gitdrive/v1/auth');
  const AUTH_CONTEXT = utf8('gitdrive-auth-v1');

  class CryptoError extends Error {}

  function fail(msg) { throw new CryptoError(msg); }

  function hex(bytes) {
    return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  function unhex(s) {
    const out = new Uint8Array(s.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
    return out;
  }

  function concat(...parts) {
    const total = parts.reduce((n, p) => n + p.length, 0);
    const out = new Uint8Array(total);
    let at = 0;
    for (const p of parts) { out.set(p, at); at += p.length; }
    return out;
  }

  function randomBytes(n) { return crypto.getRandomValues(new Uint8Array(n)); }
  function randomHex(n) { return hex(randomBytes(n)); }

  /* Byte-exact sibling of crypto.canonical(): sorted keys, no whitespace. */
  function canonical(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    const keys = Object.keys(value).sort();
    return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }

  async function hkdf(ikm, info, salt) {
    if (!ikm || !info) fail('hkdf requires key material and an info label');
    const key = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
    const bits = await crypto.subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt: salt || new Uint8Array(0), info },
      key, KEY_LEN * 8,
    );
    return new Uint8Array(bits);
  }

  async function aesKey(bytes) {
    return crypto.subtle.importKey('raw', bytes, 'AES-GCM', false, ['encrypt', 'decrypt']);
  }

  async function deriveKRoot(password, saltHex, params) {
    const p = { m_kib: ARGON2_M_KIB, t: ARGON2_T, p: ARGON2_P, ...(params || {}) };
    checkKdfParams({ alg: 'argon2id', salt: saltHex, ...p });
    const out = await window.hashwasm.argon2id({
      password,
      salt: unhex(saltHex),
      parallelism: p.p,
      iterations: p.t,
      memorySize: p.m_kib,
      hashLength: KEY_LEN,
      outputType: 'binary',
    });
    return new Uint8Array(out);
  }

  function checkKdfParams(params) {
    if (params.alg === 'server') return;                    // public space: no password involved
    if (params.alg !== 'argon2id') fail('unsupported kdf: ' + params.alg);
    if (unhex(params.salt).length < 16) fail('kdf salt is too short');
    if (params.m_kib < ARGON2_MIN_M_KIB || params.t < ARGON2_MIN_T) fail('kdf below the enforced floor');
    if (params.m_kib > 1048576 || params.t > 16 || params.p > 16) fail('kdf above the accepted maximum');
  }

  async function authVerifier(kRoot) {
    const kAuth = await hkdf(kRoot, INFO_AUTH);
    const mac = await crypto.subtle.sign('HMAC', await crypto.subtle.importKey(
      'raw', kAuth, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']), AUTH_CONTEXT);
    return hex(new Uint8Array(mac));
  }

  async function fileKek(rootKey, fileId) { return hkdf(rootKey, INFO_KEK, unhex(fileId)); }

  async function chunkKey(dek, index, total) {
    return hkdf(dek, enc.encode(`gitdrive/v1/chunk/${index}/${total}`));
  }

  function chunkAad(fileId, header) {
    return concat(CHUNK_AAD_PREFIX, enc.encode(fileId), header);
  }

  function chunkHeader(mode, index, total, plainLen, nonce) {
    if (!(index >= 0 && index < total)) fail('chunk index out of range');
    const h = new Uint8Array(CHUNK_HEADER_LEN);
    h.set(CHUNK_MAGIC, 0);
    h[4] = mode;
    new DataView(h.buffer).setUint32(5, index, false);
    new DataView(h.buffer).setUint32(9, total, false);
    new DataView(h.buffer).setUint32(13, plainLen, false);
    h.set(nonce, 17);
    return h;
  }

  async function sealChunk(fileId, mode, index, total, keyHex, plain, nonceHex) {
    const nonce = nonceHex ? unhex(nonceHex) : randomBytes(NONCE_LEN);
    const header = chunkHeader(mode, index, total, plain.length, nonce);
    const ct = new Uint8Array(await crypto.subtle.encrypt({
      name: 'AES-GCM', iv: nonce, additionalData: chunkAad(fileId, header),
    }, await aesKey(unhex(keyHex)), plain));
    return concat(header, ct);                        // header || ciphertext || tag
  }

  async function openChunk(fileId, blob, keyHex, expect) {
    if (blob.length < CHUNK_HEADER_LEN + TAG_LEN) fail('not a GitDrive chunk');
    const header = blob.subarray(0, CHUNK_HEADER_LEN);
    if (header[0] !== 0x47 || header[1] !== 0x44 || header[2] !== 0x43 || header[3] !== 0x31) {
      fail('not a GitDrive chunk');
    }
    const view = new DataView(header.buffer, header.byteOffset, header.byteLength);
    const index = view.getUint32(5, false);
    const total = view.getUint32(9, false);
    const plainLen = view.getUint32(13, false);
    const nonce = header.subarray(17);
    if (blob.length - CHUNK_HEADER_LEN !== plainLen + TAG_LEN) fail('chunk length does not match its header');
    if (!(index >= 0 && index < total)) fail('chunk index out of range');
    if (expect && expect.index !== undefined && index !== expect.index) fail('chunk is out of order');
    if (expect && expect.total !== undefined && total !== expect.total) fail('chunk count does not match the manifest');
    let plain;
    try {
      plain = new Uint8Array(await crypto.subtle.decrypt({
        name: 'AES-GCM', iv: nonce, additionalData: chunkAad(fileId, header),
      }, await aesKey(unhex(keyHex)), blob.subarray(CHUNK_HEADER_LEN)));
    } catch {
      fail('chunk failed authentication');
    }
    return { index, total, plainLen, tag: blob.subarray(blob.length - TAG_LEN), plain };
  }

/* Folds the per-chunk blob hashes so reordering, duplication or truncation is detectable. */
async function chainDigest(shaHexes) {
    const sha = (bytes) => crypto.subtle.digest('SHA-256', bytes);
    let digest = new Uint8Array(await sha(new Uint8Array(0)));
    for (const s of shaHexes) digest = new Uint8Array(await sha(concat(digest, unhex(s))));
    return hex(digest);
  }

  function manifestAad(manifest) {
    const { meta, key_wrap: _kw, ...rest } = manifest;
    return utf8(canonical(rest));
  }

  async function sealBytes(kek, aad, value) {
    const nonce = randomBytes(NONCE_LEN);
    const ct = new Uint8Array(await crypto.subtle.encrypt({
      name: 'AES-GCM', iv: nonce, additionalData: aad,
    }, await aesKey(kek), value));
    return { alg: 'aes-256-gcm', nonce: hex(nonce), ct: hex(ct) };
  }

  async function openBytes(kek, aad, blob) {
    if (!blob || blob.alg !== 'aes-256-gcm') fail('unsupported algorithm');
    if (typeof blob.nonce !== 'string' || typeof blob.ct !== 'string') fail('ciphertext blob is malformed');
    let plain;
    try {
      plain = new Uint8Array(await crypto.subtle.decrypt({
        name: 'AES-GCM', iv: unhex(blob.nonce), additionalData: aad,
      }, await aesKey(kek), unhex(blob.ct)));
    } catch {
      fail('wrong password, or the manifest was altered');
    }
    return plain;
  }

  async function buildManifest(opts) {
    const manifest = {
      version: FORMAT_VERSION,
      mode: opts.mode === MODE_PRIVATE ? 'private' : 'public',
      cipher: { alg: 'aes-256-gcm', nonce: NONCE_LEN, tag: TAG_LEN },
      kdf: opts.kdf,
      hkdf: { alg: 'hkdf-sha256' },
      file_id: opts.fileId,
      chunk_size: opts.chunkSize,
      chunk_count: opts.chunkCount,
      plain_size: opts.plainSize,
    };
    const aad = manifestAad(manifest);
    const kek = await fileKek(opts.kRoot, opts.fileId);
    const dek = unhex(opts.dekHex);
    const wrapped = await sealBytes(kek, aad, dek);
    manifest.key_wrap = { alg: 'aes-256-gcm', nonce: wrapped.nonce, wrapped_dek: wrapped.ct };
    manifest.meta = await sealBytes(await hkdf(kek, INFO_META), aad, utf8(canonical(opts.meta)));
    return manifest;
  }

  async function openManifest(manifest, rootKey) {
    if (manifest.version !== FORMAT_VERSION) fail('unsupported manifest version');
    if (!manifest.cipher || manifest.cipher.alg !== 'aes-256-gcm') fail('unsupported cipher');
    checkKdfParams(manifest.kdf);
    const aad = manifestAad(manifest);
    const kek = await fileKek(rootKey, manifest.file_id);
    const dek = await openBytes(kek, aad, {
      alg: manifest.key_wrap.alg, nonce: manifest.key_wrap.nonce, ct: manifest.key_wrap.wrapped_dek,
    });
    const meta = JSON.parse(dec.decode(await openBytes(await hkdf(kek, INFO_META), aad, manifest.meta)));
    return { dekHex: hex(dek), kek, meta };
  }

  return {
    FORMAT_VERSION, MODE_PRIVATE, MODE_PUBLIC,
    KEY_LEN, NONCE_LEN, TAG_LEN, CHUNK_HEADER_LEN,
    ARGON2_M_KIB, ARGON2_T, ARGON2_P,
    CryptoError, canonical, hex, unhex, randomBytes, randomHex, concat,
    hkdf, deriveKRoot, checkKdfParams, authVerifier, fileKek, chunkKey,
    chunkHeader, sealChunk, openChunk, chainDigest, buildManifest, openManifest,
  };
})();