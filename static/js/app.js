let currentUrl    = "";
let currentFormat = "mp4";
let ffmpegReady   = false;

function selectFormat(btn) {
  document.querySelectorAll(".ftab").forEach(t => t.classList.remove("active"));
  btn.classList.add("active");
  currentFormat = btn.dataset.f;
  const badge = document.getElementById("fmt-badge");
  if (badge) badge.textContent = currentFormat.toUpperCase();
}

async function handleConvert() {
  const input   = document.getElementById("url-input");
  const btn     = document.getElementById("convert-btn");
  const errorEl = document.getElementById("error-msg");
  const box     = document.getElementById("search-box");

  currentUrl = input.value.trim();
  errorEl.textContent = "";
  box.classList.remove("error");

  if (!currentUrl) { showError("Please paste a YouTube URL."); return; }

  btn.textContent = "Converting…";
  btn.disabled    = true;
  showLoading(true);

  try {
    const res  = await fetch("/api/convert", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ url: currentUrl, format: currentFormat }),
    });
    const data = await res.json();

    if (!res.ok) {
      showError(data.detail || "Something went wrong.");
      hideResultCard();
      return;
    }

    ffmpegReady = data.ffmpeg_ready;
    renderVideoMeta(data.video, data.format);
    renderQualities(data.qualities, data.ffmpeg_ready);
    showLoading(false);

  } catch {
    showError("Network error — is the server running?");
    hideResultCard();
  } finally {
    btn.textContent = "Convert";
    btn.disabled    = false;
  }
}

async function handleDownload(btn, quality) {
  const origHTML = btn.innerHTML;
  setDlState(btn, "loading", "Merging…");

  try {
    const res = await fetch("/api/download", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ url: currentUrl, quality, format: currentFormat }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: "Server error" }));
      showError(err.detail || "Download failed.");
      setDlState(btn, "error", "Error");
      setTimeout(() => resetDlBtn(btn, origHTML), 3000);
      return;
    }

    setDlState(btn, "loading", "Saving…");
    const blob = await res.blob();
    const cd   = res.headers.get("Content-Disposition") || "";
    const m    = cd.match(/filename="?([^"]+)"?/);
    const fname = m ? m[1] : `video.${currentFormat}`;

    const blobUrl = URL.createObjectURL(blob);
    const a       = document.createElement("a");
    a.href        = blobUrl;
    a.download    = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(blobUrl), 10000);

    setDlState(btn, "ready", "Done!");
    setTimeout(() => resetDlBtn(btn, origHTML), 3000);

  } catch {
    showError("Network error during download.");
    setDlState(btn, "error", "Failed");
    setTimeout(() => resetDlBtn(btn, origHTML), 3000);
  }
}

function renderVideoMeta(video, format) {
  document.getElementById("vid-title").textContent  = video.title || "Unknown";
  document.getElementById("vid-sub").textContent    =
    `${video.channel || "?"} · ${video.duration || "—"} · ${video.views || ""}`;
  document.getElementById("fmt-badge").textContent  = format.toUpperCase();

  const img = document.getElementById("thumb-img");
  if (video.thumbnail) {
    img.src     = video.thumbnail;
    img.onerror = () => { img.style.display = "none"; };
  }
}

function renderQualities(qualities, hasFfmpeg) {
  const list = document.getElementById("quality-list");
  list.innerHTML = "";

  if (!hasFfmpeg) {
    const warn = document.createElement("div");
    warn.style.cssText =
      "padding:10px 16px;font-size:12px;color:#c0a940;"
      + "background:#2a2a1a;border-bottom:0.5px solid #3a3a2a;"
      + "display:flex;align-items:center;gap:8px;";
    warn.innerHTML =
      `<svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <path d="M7 1L13 12H1L7 1Z" stroke="#c0a940" stroke-width="1.2"
              stroke-linejoin="round"/>
        <line x1="7" y1="5" x2="7" y2="8" stroke="#c0a940"
              stroke-width="1.2" stroke-linecap="round"/>
        <circle cx="7" cy="10" r="0.6" fill="#c0a940"/>
      </svg>
      <span>ffmpeg not found — only 360p available. 
        <a href="https://www.gyan.dev/ffmpeg/builds/" target="_blank"
           style="color:#e0c060;text-decoration:underline;">
          Install ffmpeg
        </a> to unlock all qualities.</span>`;
    list.appendChild(warn);
  }

  qualities.forEach(q => {
    const unavailable = !q.available;
    const row         = document.createElement("div");
    row.className     = "quality-row" + (unavailable ? " unavailable" : "");

    let btnHtml;
    if (unavailable && q.needs_ffmpeg) {
      btnHtml = `<button class="btn-dl needs-ffmpeg" disabled
                   title="Requires ffmpeg — see warning above">
                   Needs ffmpeg
                 </button>`;
    } else if (unavailable) {
      btnHtml = `<button class="btn-dl" disabled
                   style="background:#333;cursor:not-allowed;opacity:0.5">
                   Unavailable
                 </button>`;
    } else {
      btnHtml = `<button class="btn-dl" onclick="handleDownload(this,'${q.res}')">
                   ${dlIcon()} Download
                 </button>`;
    }

    row.innerHTML = `
      <span class="q-res"  style="${unavailable ? 'color:#555' : ''}">${q.res}</span>
      <span class="q-label">${q.label}</span>
      <span class="q-size">${q.size}</span>
      <span class="q-badge ${q.badge_class}">${q.badge}</span>
      ${btnHtml}`;
    list.appendChild(row);
  });
}

function dlIcon() {
  return `<svg width="12" height="12" viewBox="0 0 12 12" fill="none">
    <path d="M6 2v6M3 6l3 3 3-3" stroke="white" stroke-width="1.5"
          stroke-linecap="round" stroke-linejoin="round"/>
    <line x1="2" y1="10.5" x2="10" y2="10.5" stroke="white"
          stroke-width="1.5" stroke-linecap="round"/>
  </svg>`;
}

function checkIcon() {
  return `<svg width="12" height="12" viewBox="0 0 12 12" fill="none">
    <path d="M2 6.5l3 3 5-5" stroke="#5ec96a" stroke-width="1.5"
          stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
}

function spinnerIcon() {
  return `<svg width="12" height="12" viewBox="0 0 12 12" fill="none"
               style="animation:spin 0.7s linear infinite">
    <circle cx="6" cy="6" r="4" stroke="rgba(255,255,255,0.2)" stroke-width="1.5"/>
    <path d="M6 2a4 4 0 0 1 4 4" stroke="white" stroke-width="1.5"
          stroke-linecap="round"/>
  </svg>`;
}

function setDlState(btn, state, label) {
  btn.disabled  = state === "loading";
  btn.className = `btn-dl ${state}`;
  btn.innerHTML = state === "loading" ? `${spinnerIcon()} ${label}`
                : state === "ready"   ? `${checkIcon()} ${label}`
                : label;
}

function resetDlBtn(btn, html) {
  btn.className = "btn-dl";
  btn.innerHTML = html;
  btn.disabled  = false;
}

function showLoading(on) {
  const card   = document.getElementById("result-area");
  const loader = document.getElementById("loading-row");
  const list   = document.getElementById("quality-list");
  card.classList.remove("hidden");
  if (on) { loader.classList.remove("hidden"); list.innerHTML = ""; }
  else    { loader.classList.add("hidden"); }
}

function hideResultCard() {
  document.getElementById("result-area").classList.add("hidden");
  document.getElementById("loading-row").classList.add("hidden");
}

function showError(msg) {
  document.getElementById("error-msg").textContent = msg;
  document.getElementById("search-box").classList.add("error");
}

const s = document.createElement("style");
s.textContent = `
  @keyframes spin { to { transform: rotate(360deg); } }
  .quality-row.unavailable { opacity: 0.5; }
  .btn-dl.loading      { background: #444; cursor: not-allowed; }
  .btn-dl.ready        { background: #1a3a1a; }
  .btn-dl.error        { background: #3a1a1a; }
  .btn-dl.needs-ffmpeg { background: #2a2a1a; color: #c0a940;
                         border: 0.5px solid #4a4a1a; font-size: 11px; }
`;
document.head.appendChild(s);

document.getElementById("url-input")
  .addEventListener("keydown", e => { if (e.key === "Enter") handleConvert(); });