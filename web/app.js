const $ = (sel) => document.querySelector(sel);
const platformsEl = $("#platforms");
const resultsEl = $("#results");
const statusEl = $("#status");
const form = $("#search-form");
const goBtn = $("#go");

let platforms = [];
const enabled = new Set();

// ---------------------------------------------------------------- platforms
async function loadPlatforms() {
  try {
    const res = await fetch("/api/platforms");
    platforms = (await res.json()).platforms;
  } catch {
    statusEl.innerHTML = `<span class="warn">Could not reach the server.</span>`;
    return;
  }

  platformsEl.innerHTML = "";
  for (const p of platforms) {
    if (p.available) enabled.add(p.id);

    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip " + (p.available ? "on" : "disabled");
    chip.dataset.kind = p.kind;
    chip.dataset.id = p.id;
    chip.setAttribute("aria-pressed", String(p.available));
    chip.innerHTML = `<span class="dot"></span>${p.label}`;

    if (!p.available) {
      chip.disabled = true;
      chip.title = `Not configured - needs ${p.needs}`;
    } else {
      chip.addEventListener("click", () => toggle(p.id, chip));
    }
    platformsEl.appendChild(chip);
  }
}

function toggle(id, chip) {
  if (enabled.has(id)) {
    enabled.delete(id);
    chip.classList.replace("on", "off");
  } else {
    enabled.add(id);
    chip.classList.replace("off", "on");
  }
  chip.setAttribute("aria-pressed", String(enabled.has(id)));
}

// ------------------------------------------------------------------- search
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;

  if (enabled.size === 0) {
    resultsEl.innerHTML = "";
    statusEl.innerHTML = `<span class="warn">Select at least one platform.</span>`;
    return;
  }

  goBtn.disabled = true;
  resultsEl.innerHTML = "";
  const names = platforms.filter((p) => enabled.has(p.id)).map((p) => p.label).join(", ");
  statusEl.innerHTML = `<span class="spinner"></span>Searching ${names} for <strong>${escapeHtml(q)}</strong>…`;

  const company = $("#company").value.trim();
  const params = new URLSearchParams({
    q,
    company,
    strict: $("#strict").checked && company ? "1" : "",
    sources: [...enabled].join(","),
    days: $("#days").value,
  });

  try {
    const res = await fetch("/api/search?" + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Search failed");
    render(q, data);
  } catch (err) {
    statusEl.innerHTML = `<span class="warn">${escapeHtml(err.message)}</span>`;
  } finally {
    goBtn.disabled = false;
  }
});

// ------------------------------------------------------------------- render
function render(query, data) {
  const n = data.count;
  const strict = data.company_match === "require";
  const who = data.company
    ? `<strong>${escapeHtml(query)}</strong> <span class="at">(${escapeHtml(data.company)})</span>`
    : `<strong>${escapeHtml(query)}</strong>`;
  let line = n === 0
    ? `No interviews found for ${who}.`
    : `<strong>${n}</strong> interview${n === 1 ? "" : "s"} with ${who}`;
  if (n > 0) line += ` · ${data.searched.length} platform${data.searched.length === 1 ? "" : "s"} · ${data.elapsed_seconds}s`;
  if (data.errors?.length) {
    line += ` <span class="warn">· ${data.errors.length} source error${data.errors.length === 1 ? "" : "s"}</span>`;
  }
  statusEl.innerHTML = line;

  if (n === 0) {
    // Strict matching is the most likely reason a search that used to return
    // something now returns nothing, so say so before the generic advice.
    const hint = strict
      ? `Every result had to mention <strong>${escapeHtml(data.company)}</strong>.
         Untick “Company must match” to see the looser set.`
      : `Try a wider time window, add the company name to disambiguate,
         or enable more platforms.`;
    resultsEl.innerHTML = `
      <div class="empty">
        <h2>Nothing found</h2>
        <p>${hint}</p>
      </div>`;
    return;
  }

  resultsEl.innerHTML = "";
  // The first screenful loads eagerly so results look instant; the rest wait
  // until they are scrolled near.
  data.results.forEach((item, i) => resultsEl.appendChild(card(item, i < 8)));
}

function card(item, eager = false) {
  const a = document.createElement("a");
  a.className = "card";
  a.href = item.url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";

  const initial = (item.publisher || item.title || "?").trim().charAt(0).toUpperCase();
  const runtime = item.duration_seconds
    ? `<span class="runtime">${formatDuration(item.duration_seconds)}</span>` : "";

  // Flagged, never hidden. The reasons go in the title attribute so hovering
  // says why, without the card growing a paragraph.
  if (item.probably_not_real) a.classList.add("suspect");
  const suspect = item.probably_not_real
    ? `<div class="suspect-note" title="${escapeHtml((item.slop_reasons || []).join(", "))}">probably AI-generated</div>`
    : "";

  a.innerHTML = `
    <div class="thumb">
      <div class="fallback">${escapeHtml(initial)}</div>
      <span class="badge" data-kind="${escapeHtml(item.media_type)}">
        <span class="dot"></span>${escapeHtml(item.media_type)}
      </span>
      ${runtime}
    </div>
    <div class="body">
      <p class="title">${escapeHtml(item.title)}</p>
      <div class="meta">
        <span class="pub">${escapeHtml(item.publisher || hostOf(item.url))}</span>
        ${item.published ? `<span class="sep">·</span><time datetime="${escapeHtml(item.published_iso)}">${escapeHtml(item.published)}</time>` : ""}
      </div>
      ${suspect}
      <div class="link">${escapeHtml(hostOf(item.url))}</div>
    </div>`;

  // The image sits in the DOM layered over the letter placeholder, so lazy
  // loading works (a detached image is never in the viewport and so never
  // loads). If the URL is dead, drop the image and the placeholder shows.
  if (item.thumbnail) {
    const img = document.createElement("img");
    img.alt = "";
    img.loading = eager ? "eager" : "lazy";
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.remove());
    img.src = item.thumbnail;
    a.querySelector(".thumb").appendChild(img);
  }
  return a;
}

// -------------------------------------------------------------------- utils
function formatDuration(s) {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}

function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); }
  catch { return ""; }
}

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

loadPlatforms();
