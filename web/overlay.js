(() => {
  "use strict";

  const apiBase = String(window.OVERSEER_API_BASE_URL || "").replace(/\/+$/, "");
  const apiUrl = (path) => `${apiBase}${path}`;
  const active = new Map();
  const pending = new Map();
  const pinned = new Set();
  const expiry = new Map();
  let matchClock = 0;
  let retryTimer = 0;
  const stack = document.querySelector("#overlay-stack");
  const svg = document.querySelector("#marking-layer");
  const connection = document.querySelector("#connection-state");

  function tcToSeconds(timecode) {
    const match = /^(\d+):(\d{2}):(\d{2})\.(\d{3})$/.exec(String(timecode || ""));
    if (!match) return 0;
    return Number(match[1]) * 3600 + Number(match[2]) * 60 +
      Number(match[3]) + Number(match[4]) / 1000;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function renderActive() {
    const overlays = [...active.values()].sort((a, b) =>
      Number(pinned.has(b.overlay_id)) - Number(pinned.has(a.overlay_id)) ||
      b.priority - a.priority || b.seq - a.seq
    ).slice(0, 3);
    stack.innerHTML = overlays.map((overlay) => `
      <article class="broadcast-card ${Number(overlay.priority) >= 70 ? "priority-high" : ""} ${pinned.has(overlay.overlay_id) ? "pinned" : ""}" data-overlay-id="${escapeHtml(overlay.overlay_id)}"
      >
        <div class="broadcast-actions">
          <button type="button" data-overlay-action="pin" aria-label="${pinned.has(overlay.overlay_id) ? "Unpin" : "Pin"} insight">${pinned.has(overlay.overlay_id) ? "◆" : "◇"}</button>
          <button type="button" data-overlay-action="close" aria-label="Dismiss insight">×</button>
        </div>
        <div class="broadcast-meta"><span>${escapeHtml(overlay.type)} · ${Number(overlay.priority) >= 70 ? "KEY MOMENT" : "MATCH UPDATE"}</span><span>${escapeHtml(overlay.start_tc)}</span></div>
        <h1>${escapeHtml(overlay.headline)}</h1>
        ${overlay.body ? `<p>${escapeHtml(overlay.body)}</p>` : ""}
        ${overlay.stat ? `<span class="broadcast-stat">${escapeHtml(overlay.stat.label)} · ${escapeHtml(overlay.stat.value)}</span>` : ""}
      </article>
    `).join("");
    renderMarkings(overlays);
  }

  function renderMarkings(overlays) {
    const items = overlays.flatMap((overlay) =>
      (overlay.markings || []).map((marking) => ({ overlay, marking }))
    );
    svg.innerHTML = items.map(({ overlay, marking }) => {
      const points = marking.points || [];
      if (!points.length) return "";
      const coordinates = points.map((point) => `${Number(point.x)},${Number(point.y)}`).join(" ");
      if (marking.kind === "spotlight") {
        const point = points[0];
        return `<circle cx="${Number(point.x)}" cy="${Number(point.y)}" r="2.1"
          fill="none" stroke="#b6f35b" stroke-width=".28" opacity=".92"/>`;
      }
      if (marking.kind === "zone") {
        return `<polygon points="${coordinates}" fill="#b6f35b" fill-opacity=".12"
          stroke="#b6f35b" stroke-width=".28"/>`;
      }
      if (marking.kind === "line") {
        return `<polyline points="${coordinates}" fill="none" stroke="#b6f35b"
          stroke-width=".32" stroke-dasharray="1.2 .8"/>`;
      }
      return `<polyline points="${coordinates}" fill="none" stroke="#b6f35b"
        stroke-width=".42" stroke-linecap="round" stroke-linejoin="round"/>`;
    }).join("");
  }

  function activate(overlay) {
    if (!overlay || typeof overlay.overlay_id !== "string" || active.has(overlay.overlay_id)) return;
    pending.delete(overlay.overlay_id);
    const expiresAt = Date.now() + Math.max(250, Number(overlay.duration_ms) || 8000);
    expiry.set(overlay.overlay_id, expiresAt);
    active.set(overlay.overlay_id, overlay);
    renderActive();
    window.setTimeout(() => {
      if (!active.has(overlay.overlay_id)) return;
      if (pinned.has(overlay.overlay_id)) return;
      active.delete(overlay.overlay_id);
      expiry.delete(overlay.overlay_id);
      const card = [...stack.children].find((item) =>
        item.dataset.overlayId === overlay.overlay_id
      );
      if (card) {
        card.classList.add("exiting");
        window.setTimeout(renderActive, 260);
      } else {
        renderActive();
      }
    }, Math.max(0, expiresAt - Date.now()));
  }

  function acceptOverlay(overlay) {
    if (!overlay || typeof overlay.overlay_id !== "string") return;
    pending.set(overlay.overlay_id, overlay);
    scheduleReadyOverlays();
  }

  function scheduleReadyOverlays() {
    for (const [id, overlay] of pending) {
      if (tcToSeconds(overlay.start_tc) <= matchClock + 0.05) activate(overlay);
    }
  }

  function acceptSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return;
    if (snapshot.language) {
      document.documentElement.dir = snapshot.language === "Arabic" ? "rtl" : "ltr";
      document.documentElement.lang = snapshot.language === "Arabic" ? "ar" : snapshot.language === "Spanish" ? "es" : snapshot.language === "French" ? "fr" : "en";
    }
    const snapshotClock = Number(snapshot.match_clock);
    const updatedClock = Number.isFinite(snapshotClock)
      ? snapshotClock
      : Number(snapshot.minute || 0) * 60;
    matchClock = updatedClock < matchClock - 5 ? updatedClock : Math.max(matchClock, updatedClock);
    scheduleReadyOverlays();
  }

  async function hydrate() {
    try {
      const [snapshotResponse, recentResponse] = await Promise.all([
        fetch(apiUrl("/state")),
        fetch(apiUrl("/overlays/recent?limit=100"))
      ]);
      if (!snapshotResponse.ok || !recentResponse.ok) {
        throw new Error("Unable to load current match state and recent overlays");
      }
      acceptSnapshot(await snapshotResponse.json());
      (await recentResponse.json()).forEach(acceptOverlay);
      if (retryTimer) {
        window.clearTimeout(retryTimer);
        retryTimer = 0;
      }
    } catch (error) {
      connection.textContent = "RECONNECTING";
      console.error("Could not hydrate overlay state", error);
      if (!retryTimer) retryTimer = window.setTimeout(() => {
        retryTimer = 0;
        void hydrate();
      }, 3000);
    }
  }

  function connect() {
    const stream = new EventSource(apiUrl("/stream"));
    stream.onopen = () => {
      connection.textContent = "LIVE";
      void hydrate();
    };
    stream.onmessage = (event) => {
      try { acceptSnapshot(JSON.parse(event.data)); }
      catch (error) { console.error("Invalid state snapshot", error); }
    };
    stream.addEventListener("overlay", (event) => {
      try { acceptOverlay(JSON.parse(event.data)); }
      catch (error) { console.error("Invalid overlay message", error); }
    });
    stream.onerror = () => { connection.textContent = "RECONNECTING"; };
    void hydrate();
  }

  stack.addEventListener("click", (event) => {
    const card = event.target.closest("[data-overlay-id]");
    if (!card) return;
    const id = card.dataset.overlayId;
    const action = event.target.closest("[data-overlay-action]")?.dataset.overlayAction;
    if (action === "close") {
      pinned.delete(id);
      expiry.delete(id);
      active.delete(id);
    } else if (pinned.has(id)) {
      pinned.delete(id);
      if ((expiry.get(id) || 0) <= Date.now()) {
        expiry.delete(id);
        active.delete(id);
      }
    } else {
      pinned.add(id);
    }
    renderActive();
  });
  connect();
})();
