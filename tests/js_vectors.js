/* Cross-implementation check: runs gitdrive-crypto.js under Node's Web Crypto and
 * compares it against the vectors that api/crypto.py produced.
 *
 *   node tests/js_vectors.js
 *
 * Needs hash-wasm's browser build; point HASH_WASM at argon2.umd.min.js (or drop it in
 * ./node_modules/hash-wasm/dist/). tests/test_vectors.py drives this and skips when it is
 * not available.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const V = JSON.parse(fs.readFileSync(path.join(REPO, 'tests', 'vectors.json'), 'utf8'));
const HASH_WASM = process.env.HASH_WASM ||
  path.join(REPO, 'node_modules', 'hash-wasm', 'dist', 'argon2.umd.min.js');

// Evaluated in this realm on purpose: a vm-context would hand Web Crypto cross-realm
// TypedArrays, which it rejects. A browser has exactly one realm, so this matches it.
const load = (file) => (0, eval)(fs.readFileSync(file, 'utf8'));
globalThis.window = globalThis;
globalThis.self = globalThis;
load(HASH_WASM);
load(path.join(REPO, 'gitdrive-crypto.js'));

const G = globalThis.GDCrypto;
const dec = new TextDecoder();
const enc = new TextEncoder();
const kdfArgs = { m_kib: V.kdf.m_kib, t: V.kdf.t, p: V.kdf.p };
let failures = 0;

function eq(label, got, want) {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}`);
  if (!ok) console.log(`          got  ${got}\n          want ${want}`);
}

(async () => {
  const unhex = G.unhex, hex = G.hex;

  const kroot = await G.deriveKRoot(V.password, V.salt_hex, kdfArgs);
  eq('Argon2id(password, salt) == Python K_root', hex(kroot), V.kroot_hex);
  eq('fileKek(K_root, file_id)', hex(await G.fileKek(kroot, V.file_id)), V.kek_hex);
  eq('chunkKey(DEK, 1, 2)', hex(await G.chunkKey(unhex(V.dek_hex), 1, 2)), V.chunk_key_hex);
  eq('authVerifier(K_root)', await G.authVerifier(kroot), V.auth_verifier);

  // canonical() must agree byte for byte, including the non-ASCII filename.
  eq('canonical(meta) == Python meta plaintext',
     hex(enc.encode(G.canonical(V.meta_object))), V.meta_plaintext_hex);

  const manifest = await G.buildManifest({
    mode: G.MODE_PRIVATE, fileId: V.file_id, chunkSize: 10485760, chunkCount: 2,
    plainSize: 20000000, kRoot: kroot, dekHex: V.dek_hex, kdf: V.kdf, meta: V.meta_object,
  });
  const { meta, key_wrap, ...rest } = manifest;
  eq('manifest AAD identical to Python',
     hex(enc.encode(G.canonical(rest))), V.manifest_aad_hex);

  const blob = await G.sealChunk(V.file_id, G.MODE_PRIVATE, 1, 2, V.chunk_key_hex,
                                  enc.encode('payload'), V.nonce_hex);
  eq('sealChunk == Python chunk blob', hex(blob), V.chunk_blob_hex);

  const opened = await G.openChunk(V.file_id, unhex(V.chunk_blob_hex), V.chunk_key_hex,
                                   { index: 1, total: 2 });
  eq('openChunk plaintext', dec.decode(opened.plain), 'payload');

  // A manifest produced by Python must open here.
  const opened2 = await G.openManifest(V.manifest, kroot);
  eq('openManifest(Python manifest) -> DEK', opened2.dekHex, V.dek_hex);
  eq('openManifest(Python manifest) -> meta name', opened2.meta.name, V.meta_object.name);

  // And a manifest produced here must open in Python (checked by test_vectors.py).
  if (!fs.existsSync(path.join(REPO, 'tests', 'js_manifest.json'))) {
    fs.writeFileSync(path.join(REPO, 'tests', 'js_manifest.json'), JSON.stringify(manifest));
  }

  const rejects = async (label, fn) => {
    let threw = false;
    try { await fn(); } catch (e) { threw = e instanceof G.CryptoError; }
    eq(label, String(threw), 'true');
  };

  const wrongRoot = await G.deriveKRoot('not the password', V.salt_hex, kdfArgs);
  await rejects('wrong password rejected with CryptoError',
    () => G.openManifest(V.manifest, wrongRoot));

  const tampered = unhex(V.chunk_blob_hex);
  tampered[40] ^= 0x01;
  await rejects('tampered chunk rejected with CryptoError',
    () => G.openChunk(V.file_id, tampered, V.chunk_key_hex));

  await rejects('chunk from another file rejected (AAD binds file_id)',
    () => G.openChunk('ff'.repeat(16), unhex(V.chunk_blob_hex), V.chunk_key_hex));

  const weak = JSON.parse(JSON.stringify(V.manifest));
  weak.kdf = { ...weak.kdf, m_kib: 8, t: 1 };
  await rejects('downgraded kdf parameters refused',
    () => G.checkKdfParams(weak.kdf));

  const a = await G.sealChunk(V.file_id, G.MODE_PRIVATE, 0, 2, V.chunk_key_hex, enc.encode('same'));
  const b = await G.sealChunk(V.file_id, G.MODE_PRIVATE, 0, 2, V.chunk_key_hex, enc.encode('same'));
  eq('fresh random nonce per chunk (blobs differ)', String(hex(a) !== hex(b)), 'true');

  eq('chainDigest matches Python', await G.chainDigest([]),
     require('crypto').createHash('sha256').update(Buffer.alloc(0)).digest('hex'));

  console.log(failures ? `\n${failures} FAILURE(S)`
                       : '\nJS implementation agrees with Python on every vector');
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });