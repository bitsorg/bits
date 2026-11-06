// bits-admin's page: enrol a key, a security key or a synced passkey (WebAuthn with PRF),
// store the signer's passphrase encrypted with a key derived from its PRF output, unlock.
"use strict";
const enc = new TextEncoder();
const SALT = enc.encode("bits-admin unlock v1");  // the same at every unlock
const b64u = buf => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64u = s => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));

function say(text) { document.getElementById("status").textContent = text; }

async function post(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
                               body: JSON.stringify(body), credentials: "same-origin" });
  const answer = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(answer.error || r.status + " " + r.statusText);
  return answer;
}

async function enrol(event) {
  event.preventDefault();
  const label = document.getElementById("label").value;
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const o = await post("/passkeys/options", { label });
    o.challenge = unb64u(o.challenge);
    o.user.id = unb64u(o.user.id);
    o.excludeCredentials = (o.excludeCredentials || []).map(c => Object.assign(c, { id: unb64u(c.id) }));
    o.extensions = { prf: {} };
    say("Confirm with your key (touch, fingerprint or face)…");
    const c = await navigator.credentials.create({ publicKey: o });
    await post("/passkeys", { credential: {
      id: c.id, rawId: b64u(c.rawId), type: c.type,
      authenticatorAttachment: c.authenticatorAttachment,
      clientExtensionResults: c.getClientExtensionResults(),
      response: { clientDataJSON: b64u(c.response.clientDataJSON),
                  attestationObject: b64u(c.response.attestationObject),
                  transports: c.response.getTransports ? c.response.getTransports() : [] } } });
    location.reload();
  } catch (e) {
    say(e.name === "NotAllowedError" ? "Cancelled, timed out or not allowed: " + e.message : e.message);
  } finally {
    button.disabled = false;
  }
}

// AES-GCM key from the PRF output (HKDF-SHA-256); it never leaves the page.
async function boxKey(prfOutput) {
  const base = await crypto.subtle.importKey("raw", prfOutput, "HKDF", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: new Uint8Array(32),
                                   info: enc.encode("bits-admin passphrase") },
                                 base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
}

// Asks the key (user verification, PRF); returns [assertion for the server, AES key].
async function touch(optionsUrl, id) {
  const o = await post(optionsUrl, { id });
  o.challenge = unb64u(o.challenge);
  o.allowCredentials = (o.allowCredentials || []).map(c => Object.assign(c, { id: unb64u(c.id) }));
  o.extensions = { prf: { eval: { first: SALT } } };
  say("Confirm with your key (touch, fingerprint or face)…");
  const a = await navigator.credentials.get({ publicKey: o });
  const prf = a.getClientExtensionResults().prf;
  if (!prf || !prf.results || !prf.results.first) throw new Error("the key gave no PRF output");
  return [assertionBody(a), await boxKey(prf.results.first)];
}

// What the server checks; never the PRF output.
function assertionBody(a) {
  return { id: a.id, rawId: b64u(a.rawId), type: a.type, clientExtensionResults: {},
    authenticatorAttachment: a.authenticatorAttachment,
    response: { clientDataJSON: b64u(a.response.clientDataJSON),
                authenticatorData: b64u(a.response.authenticatorData),
                signature: b64u(a.response.signature),
                userHandle: a.response.userHandle ? b64u(a.response.userHandle) : null } };
}

async function signIn(event) {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const o = await post("/signin/options", {});
    const ticket = o.ticket;
    delete o.ticket;
    o.challenge = unb64u(o.challenge);
    o.allowCredentials = (o.allowCredentials || []).map(c => Object.assign(c, { id: unb64u(c.id) }));
    say("Confirm with your key (touch, fingerprint or face)…");
    const a = await navigator.credentials.get({ publicKey: o });
    await post("/signin", { ticket, credential: assertionBody(a) });
    location.reload();
  } catch (e) {
    say(e.name === "NotAllowedError" ? "Cancelled, timed out or not allowed: " + e.message : e.message);
  } finally {
    button.disabled = false;
  }
}

async function storePassphrase(event) {
  event.preventDefault();
  const id = document.getElementById("box-key").value;
  const input = document.getElementById("passphrase");
  const button = event.target.querySelector("button");
  if (!input.value) return say("Type the passphrase first.");
  button.disabled = true;
  try {
    const [assertion, key] = await touch("/passkeys/box-options", id);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: enc.encode(id) },
                                           key, enc.encode(input.value));
    const back = await crypto.subtle.decrypt({ name: "AES-GCM", iv, additionalData: enc.encode(id) }, key, ct);
    if (new TextDecoder().decode(back) !== input.value) throw new Error("encryption check failed; nothing stored");
    say("Checking the passphrase…");
    await post("/passkeys/box", { credential: assertion, passphrase: input.value, iv: b64u(iv), ct: b64u(ct) });
    input.value = "";
    location.reload();
  } catch (e) {
    say(e.name === "NotAllowedError" ? "Cancelled, timed out or not allowed: " + e.message : e.message);
  } finally {
    button.disabled = false;
  }
}

// Touch, fetch the stored copy, decrypt it here, send the passphrase on to the helper.
async function unlock(event) {
  event.preventDefault();
  const id = document.getElementById("unlock-key").value;
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    const [assertion, key] = await touch("/unlock/options", id);
    const box = await post("/unlock/box", { credential: assertion });
    let plain;
    try {
      plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64u(box.iv), additionalData: enc.encode(id) },
                                          key, unb64u(box.ct));
    } catch (e) {
      throw new Error("the copy stored under this key does not decrypt; store the passphrase again");
    }
    say("Unlocking…");
    const r = await post("/unlock", { passphrase: new TextDecoder().decode(plain) });
    say("Unlocked: signing key " + r.key_id16);
    setTimeout(() => location.reload(), 1500);
  } catch (e) {
    say(e.name === "NotAllowedError" ? "Cancelled, timed out or not allowed: " + e.message : e.message);
  } finally {
    button.disabled = false;
  }
}

for (const [id, handler] of [["signin", signIn], ["enrol", enrol], ["box", storePassphrase], ["unlock", unlock]]) {
  const form = document.getElementById(id);
  if (form) form.addEventListener("submit", handler);
}
