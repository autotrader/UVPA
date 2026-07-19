#!/usr/bin/env node
// Verschlüsselt die Kern-PDFs (VO_/BL_/EI_/NI_) für das statische Hosting.
// Gleicher Aufbau wie encrypt.mjs ([16 B Salt][12 B IV][GCM-Ciphertext]),
// aber: EIN PBKDF2-Schlüssel pro Lauf (gemeinsamer Salt, frischer Zufalls-IV
// pro Datei) — das Frontend cached den abgeleiteten Schlüssel pro Salt.
//
// Aufruf:  SITE_PASSWORD=… node tools/encrypt-docs.mjs <repoRoot> <outDir>

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative, dirname, basename } from "node:path";
import { randomBytes, webcrypto } from "node:crypto";

const { subtle } = webcrypto;
const [, , repoRoot, outDir] = process.argv;
const password = process.env.SITE_PASSWORD;

if (!repoRoot || !outDir || !password) {
  console.error("Usage: SITE_PASSWORD=… node tools/encrypt-docs.mjs <repoRoot> <outDir>");
  process.exit(1);
}

const HOSTED = /^(VO_|BL_|EI_|NI_).*\.pdf$/;
const SKIP_DIRS = new Set([".git", ".github", ".githooks", "GraphBuilder", "web", "tools", "node_modules"]);

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (!SKIP_DIRS.has(entry.name)) yield* walk(p);
    } else if (HOSTED.test(entry.name)) {
      yield p;
    }
  }
}

const salt = randomBytes(16);
const material = await subtle.importKey(
  "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveKey"]);
const key = await subtle.deriveKey(
  { name: "PBKDF2", salt, iterations: 310_000, hash: "SHA-256" },
  material, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);

let n = 0, bytes = 0;
const t0 = Date.now();
for (const file of walk(repoRoot)) {
  const rel = relative(repoRoot, file).replaceAll("\\", "/");
  const out = join(outDir, rel + ".enc");
  mkdirSync(dirname(out), { recursive: true });
  const iv = randomBytes(12);
  const plain = readFileSync(file);
  const ct = new Uint8Array(await subtle.encrypt({ name: "AES-GCM", iv }, key, plain));
  writeFileSync(out, Buffer.concat([salt, iv, Buffer.from(ct)]));
  n++; bytes += ct.length + 28;
  if (n % 500 === 0) console.log(`  … ${n} Dateien (${(bytes / 1048576).toFixed(0)} MB)`);
}
console.log(`${basename(outDir)}: ${n} PDFs verschlüsselt, ` +
  `${(bytes / 1048576).toFixed(0)} MB in ${((Date.now() - t0) / 1000).toFixed(0)} s.`);
