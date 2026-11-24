/* CareVoice Clinic dashboard.
   Voice call: WebRTC (SmallWebRTC) to the Pipecat pipeline when API keys
   are configured. Demo call: browser speech recognition + the same booking
   tools over /api/chat, so the flow works with no keys at all. */
const $ = (id) => document.getElementById(id);
const state = {
  voiceReady: false, sessionId: null, mode: "demo",
  pc: null, micStream: null, muted: false, timerInt: null, startedAt: 0,
  audioCtx: null, analyser: null, waveRAF: null, speaking: false,
  lastApptIds: new Set(), feedSeen: 0, recog: null, synthSpeaking: false,
};

const fmtTime = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

/* ---------- data ---------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function loadStatus() {
  try {
    const s = await api("/api/status");
    state.voiceReady = s.voice_ready;
    const pill = $("modePill");
    pill.textContent = s.voice_ready ? "● Voice agent ready" : "● Demo mode — text";
    pill.className = "pill " + (s.voice_ready ? "live" : "demo");
  } catch { $("modePill").textContent = "Server offline"; }
}

async function loadDoctors() {
  const { doctors } = await api("/api/doctors");
  let free = 0;
  $("doctorsList").innerHTML = doctors.map((d) => {
    free += Object.values(d.slots_by_date || {}).flat().length;
    $("slotDateLabel").textContent = "Tomorrow · " + Object.keys(d.slots_by_date || {})[0];
    const slots = d.free_slots.length
      ? d.free_slots.slice(0, 12).map((t) =>
          `<button class="slot" data-doctor="${d.name}" data-time="${t}">${t}</button>`).join("")
      : `<span class="slot none">Fully booked</span>`;
    return `<div class="doctor">
      <div class="doctor-top"><strong>${d.name}</strong><span class="fee">₹${d.fee_inr} / visit</span></div>
      <p class="spec">${d.speciality} · ${d.qualification} · ${d.experience_years} yrs</p>
      <div class="slots">${slots}</div>
    </div>`;
  }).join("");
  $("statDoctors").textContent = doctors.length;
  $("statSlots").textContent = free;
  document.querySelectorAll(".slot[data-time]").forEach((b) =>
    b.addEventListener("click", () => {
      $("typeBox").value = `${b.dataset.doctor}, tomorrow ${b.dataset.time}`;
      if ($("phoneOverlay").hidden) openPhone();
    }));
}

async function loadAppointments(flashId) {
  const { appointments } = await api("/api/appointments");
  const booked = appointments.filter((a) => a.status === "booked");
  $("statBooked").textContent = booked.length;
  $("statNext").textContent = booked.length
    ? `${booked[0].date.slice(5)} ${booked[0].time}` : "—";
  $("apptsEmpty").style.display = appointments.length ? "none" : "block";
  $("apptsBody").innerHTML = appointments.map((a) => `
    <tr data-id="${a.id}" class="${a.id === flashId ? "flash" : ""}">
      <td>${a.id}</td><td>${a.patient_name}<br><span style="color:var(--muted)">+91 ${a.phone}</span></td>
      <td>${a.doctor}<br><span style="color:var(--muted)">${a.speciality}</span></td>
      <td>${a.date}<br>${a.time}</td>
      <td><span class="status ${a.status}">${a.status}</span></td>
    </tr>`).join("");
  appointments.forEach((a) => state.lastApptIds.add(a.id));
}

/* ---------- sounds & waveform ---------- */
function beep(freq, dur, when = 0) {
  state.audioCtx = state.audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  const o = state.audioCtx.createOscillator(), g = state.audioCtx.createGain();
  o.frequency.value = freq; o.connect(g); g.connect(state.audioCtx.destination);
  g.gain.setValueAtTime(0.0001, state.audioCtx.currentTime + when);
  g.gain.exponentialRampToValueAtTime(0.12, state.audioCtx.currentTime + when + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, state.audioCtx.currentTime + when + dur);
  o.start(state.audioCtx.currentTime + when); o.stop(state.audioCtx.currentTime + when + dur);
}
const ringSound = () => { beep(880, 0.35, 0); beep(880, 0.35, 0.5); };
const endSound = () => beep(330, 0.4);

function startWave() {
  const canvas = $("wave"), ctx = canvas.getContext("2d");
  const draw = () => {
    state.waveRAF = requestAnimationFrame(draw);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const bars = 48, mid = canvas.height / 2;
    let level = 0.12;
    if (state.analyser) {
      const data = new Uint8Array(state.analyser.frequencyBinCount);
      state.analyser.getByteTimeDomainData(data);
      level = data.reduce((s, v) => s + Math.abs(v - 128), 0) / data.length / 40 + 0.08;
    } else if (state.synthSpeaking || state.speaking) {
      level = 0.35 + Math.random() * 0.35;
    }
    ctx.fillStyle = "#c07f1f";
    for (let i = 0; i < bars; i++) {
      const h = Math.max(3, Math.sin(i * 0.55 + Date.now() / 260) * 6 + level * 44 * (0.5 + Math.random() * 0.5));
      ctx.fillRect(i * 6.4 + 2, mid - h / 2, 3, h);
    }
  };
  draw();
}
function stopWave() { cancelAnimationFrame(state.waveRAF); }

/* ---------- phone ---------- */
function openPhone() {
  $("phoneOverlay").hidden = false;
  $("phoneTranscript").innerHTML = "";
  $("confirmCard").hidden = true;
  $("callTimer").textContent = "00:00";
}
function setCallState(t) { $("callState").textContent = t; }
function addMsg(who, text) {
  const el = document.createElement("div");
  el.className = "msg " + who; el.textContent = text;
  $("phoneTranscript").appendChild(el);
  $("phoneTranscript").scrollTop = $("phoneTranscript").scrollHeight;
}
function startTimer() {
  state.startedAt = Date.now();
  state.timerInt = setInterval(() => {
    $("callTimer").textContent = fmtTime(Math.floor((Date.now() - state.startedAt) / 1000));
  }, 1000);
}
function showConfirm(kind, data) {
  const card = $("confirmCard");
  if (kind === "booked") {
    card.innerHTML = `<strong>✓ Appointment booked</strong>${data.doctor} · ${data.date} · ${data.time}<br>Appointment ID: ${data.appointment_id} · Fee ₹${data.fee_inr}`;
  } else {
    card.innerHTML = `<strong>Appointment cancelled</strong>${data.doctor} · ${data.date} · ${data.time}<br>Appointment ID: ${data.appointment_id}`;
  }
  card.hidden = false;
  loadAppointments(data.appointment_id);
}

async function pollFeed() {
  if (!state.sessionId || $("phoneOverlay").hidden) return;
  try {
    const feed = await api(`/api/feed?session_id=${state.sessionId}`);
    feed.transcript.slice(state.feedSeen).forEach((l) => addMsg(l.who === "agent" ? "agent" : "caller", l.text));
    state.feedSeen = feed.transcript.length;
    feed.events.slice(state.feedEventsSeen || 0).forEach((e) => {
      if (e.kind === "booked") showConfirm("booked", e.data);
      if (e.kind === "cancelled") showConfirm("cancelled", e.data);
    });
    state.feedEventsSeen = feed.events.length;
  } catch { /* keep polling */ }
}

async function startCall() {
  state.sessionId = "call-" + Math.random().toString(36).slice(2, 9);
  state.feedSeen = 0;
  openPhone();
  setCallState("Ringing…");
  ringSound();
  state.mode = state.voiceReady ? "voice" : "demo";
  $("phoneMode").textContent = state.mode === "voice"
    ? "Live voice · Pipecat + ElevenLabs + Gemini"
    : "Demo mode · browser speech + same booking tools — add API keys in .env for the full voice pipeline";
  startWave();
  setTimeout(() => { if (!$("phoneOverlay").hidden) setCallState("Connecting…"); }, 1200);

  if (state.mode === "voice") return startVoiceCall();
  return startDemoCall();
}

async function startVoiceCall() {
  try {
    state.micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    state.audioCtx = state.audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const src = state.audioCtx.createMediaStreamSource(state.micStream);
    state.analyser = state.audioCtx.createAnalyser(); state.analyser.fftSize = 512;
    src.connect(state.analyser);

    const pc = new RTCPeerConnection();
    state.pc = pc;
    state.micStream.getTracks().forEach((t) => pc.addTrack(t, state.micStream));
    pc.addTransceiver("audio", { direction: "sendrecv" });
    pc.ontrack = (e) => { $("remoteAudio").srcObject = e.streams[0]; state.speaking = true; };
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const answer = await api("/api/offer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
    });
    state.sessionId = answer.session_id || state.sessionId;
    await pc.setRemoteDescription(new RTCSessionDescription(answer));
    pc.onicecandidate = (e) => {
      if (e.candidate) fetch("/api/offer", {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pc_id: answer.pc_id, candidates: [e.candidate.toJSON()] }),
      }).catch(() => {});
    };
    setCallState("Connected");
    startTimer();
    addMsg("system", "You are speaking to Maya. Say your phone number to begin.");
    setInterval(pollFeed, 1000);
  } catch (err) {
    setCallState("Voice failed — switched to demo");
    addMsg("system", "Microphone or voice service unavailable (" + err.message + "). Type below instead — the same booking tools run.");
    startDemoCall();
  }
}

function speak(text) {
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.onstart = () => { state.synthSpeaking = true; };
  u.onend = () => { state.synthSpeaking = false; };
  window.speechSynthesis.speak(u);
}

async function startDemoCall() {
  setCallState("Connected · demo");
  startTimer();
  setInterval(pollFeed, 1000);
  const first = await sendChat("Hello");
  // Speech recognition, if the browser supports it
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (SR) {
    const recog = new SR();
    recog.lang = "en-IN"; recog.continuous = true; recog.interimResults = false;
    recog.onresult = (e) => {
      const text = e.results[e.results.length - 1][0].transcript.trim();
      if (text) sendChat(text);
    };
    recog.onend = () => { if (!$("phoneOverlay").hidden && !state.muted) { try { recog.start(); } catch {} } };
    state.recog = recog;
    try { recog.start(); } catch {}
  }
  return first;
}

async function sendChat(text) {
  if (!text.trim()) return;
  addMsg("caller", text);
  try {
    const res = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.sessionId, message: text }),
    });
    addMsg("agent", res.reply);
    speak(res.reply);
    if (res.appointment) showConfirm("booked", res.appointment);
    if (res.cancelled) showConfirm("cancelled", res.cancelled);
    loadAppointments();
    return res;
  } catch (e) {
    addMsg("system", "Could not reach the server. Is it running?");
  }
}

function endCall() {
  endSound();
  if (state.pc) { state.pc.close(); state.pc = null; }
  if (state.micStream) { state.micStream.getTracks().forEach((t) => t.stop()); state.micStream = null; }
  if (state.recog) { try { state.recog.stop(); } catch {} state.recog = null; }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  clearInterval(state.timerInt);
  stopWave();
  setCallState("Call ended");
  setTimeout(() => { $("phoneOverlay").hidden = true; }, 900);
  loadAppointments();
  loadDoctors();
}

/* ---------- wire up ---------- */
$("tryCallBtn").addEventListener("click", startCall);
$("endBtn").addEventListener("click", endCall);
$("micBtn").addEventListener("click", () => {
  state.muted = !state.muted;
  if (state.micStream) state.micStream.getAudioTracks().forEach((t) => (t.enabled = !state.muted));
  if (state.recog) { state.muted ? state.recog.stop() : state.recog.start(); }
  $("micBtn").classList.toggle("off", state.muted);
  $("micBtn").textContent = state.muted ? "🔇" : "🎙";
});
$("speakerBtn").addEventListener("click", () => {
  $("remoteAudio").muted = !$("remoteAudio").muted;
  $("speakerBtn").classList.toggle("off", $("remoteAudio").muted);
});
const sendTyped = () => {
  const v = $("typeBox").value;
  $("typeBox").value = "";
  if (state.mode === "demo") sendChat(v);
  else addMsg("system", "In live voice mode, speak — or end the call and use demo mode to type.");
};
$("sendBtn").addEventListener("click", sendTyped);
$("typeBox").addEventListener("keydown", (e) => { if (e.key === "Enter") sendTyped(); });
$("resetBtn").addEventListener("click", async () => {
  await api("/api/reset", { method: "POST" });
  loadDoctors(); loadAppointments();
});
$("phoneOverlay").addEventListener("click", (e) => { if (e.target === $("phoneOverlay")) endCall(); });

loadStatus(); loadDoctors(); loadAppointments();
setInterval(() => { if ($("phoneOverlay").hidden) { loadDoctors(); loadAppointments(); } }, 5000);
