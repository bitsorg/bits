// bits-admin's page: enrol a security key (WebAuthn, with the PRF extension).
"use strict";
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
    say("Touch your security key…");
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

const form = document.getElementById("enrol");
if (form) form.addEventListener("submit", enrol);
