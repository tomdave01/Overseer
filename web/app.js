(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const API_BASE_URL = String(window.OVERSEER_API_BASE_URL || "").replace(/\/+$/, "");
  const apiUrl = (path) => `${API_BASE_URL}${path}`;
  const pitch = $("#pitch");
  const pitchContext = pitch.getContext("2d");
  const chart = $("#momentum-chart");
  const chartContext = chart.getContext("2d");
  const TEAM_COLORS = { Rovers: "#b6f35b", United: "#f0a66c" };
  const state = {
    events: [],
    frame: null,
    shape: {},
    patterns: {},
    impact: [],
    playerStats: [],
    playerStatsError: null,
    lastPlayerStatsAt: 0,
    playerStatsRequest: null,
    momentum: [],
    moments: [],
    overlays: [],
    selectedEvent: null,
    spotlight: null,
    toastTimer: null
  };

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("visible"), 3200);
  }

  async function api(path, options = {}) {
    const response = await fetch(apiUrl(path), {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail || `Request failed (${response.status})`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  }

  function resizeCanvas(canvas, context) {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.round(rect.width * ratio));
    canvas.height = Math.max(1, Math.round(rect.height * ratio));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width: rect.width, height: rect.height };
  }

  function drawPitch() {
    const { width, height } = resizeCanvas(pitch, pitchContext);
    const ctx = pitchContext;
    const pad = 12;
    const left = pad, top = pad, right = width - pad, bottom = height - pad;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#183222";
    ctx.fillRect(left, top, right - left, bottom - top);
    ctx.strokeStyle = "#80a38476";
    ctx.lineWidth = 1;
    ctx.strokeRect(left, top, right - left, bottom - top);
    ctx.beginPath();
    ctx.moveTo((left + right) / 2, top);
    ctx.lineTo((left + right) / 2, bottom);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc((left + right) / 2, (top + bottom) / 2, (bottom - top) * 0.13, 0, Math.PI * 2);
    ctx.stroke();
    const boxHeight = (bottom - top) * 0.48;
    const boxWidth = (right - left) * 0.16;
    ctx.strokeRect(left, (top + bottom - boxHeight) / 2, boxWidth, boxHeight);
    ctx.strokeRect(right - boxWidth, (top + bottom - boxHeight) / 2, boxWidth, boxHeight);

    const teams = state.shape.teams || {};
    for (const [team, teamData] of Object.entries(teams)) {
      const summary = teamData.out_of_possession;
      if (!summary || !Number.isFinite(summary.line_height)) continue;
      const x = team === "Rovers" ? summary.line_height : 100 - summary.line_height;
      ctx.setLineDash([4, 5]);
      ctx.strokeStyle = `${TEAM_COLORS[team]}70`;
      ctx.beginPath();
      ctx.moveTo(left + (x / 100) * (right - left), top);
      ctx.lineTo(left + (x / 100) * (right - left), bottom);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    const frame = state.frame;
    if (frame) {
      $("#pitch-empty").hidden = true;
      $("#possession-label").textContent = `POSSESSION ${frame.poss ? frame.poss.toUpperCase() : "—"}`;
      const ball = frame.ball || {};
      const ballX = left + (Number(ball.x || 0) / 100) * (right - left);
      const ballY = top + (Number(ball.y || 0) / 100) * (bottom - top);
      let closestSpotlightId = null;
      if (state.spotlight?.player && state.selectedEvent?.x != null) {
        let closestDistance = Infinity;
        for (const candidate of frame.players || []) {
          if (candidate.team !== state.selectedEvent.team) continue;
          const dx = Number(candidate.x) - Number(state.selectedEvent.x);
          const dy = Number(candidate.y) - Number(state.selectedEvent.y);
          const distance = dx * dx + dy * dy;
          if (distance < closestDistance) {
            closestDistance = distance;
            closestSpotlightId = candidate.id;
          }
        }
      }
      for (const player of frame.players || []) {
        const x = left + (Number(player.x || 0) / 100) * (right - left);
        const y = top + (Number(player.y || 0) / 100) * (bottom - top);
        const isSpotlight = state.spotlight && (
          player.id === state.spotlight.id || player.id === closestSpotlightId
        );
        ctx.beginPath();
        ctx.arc(x, y, isSpotlight ? 6 : player.role === "GK" ? 3.2 : 4.1, 0, Math.PI * 2);
        ctx.fillStyle = TEAM_COLORS[player.team] || "#ddd";
        ctx.fill();
        ctx.strokeStyle = isSpotlight ? "#ffffff" : "#101810";
        ctx.lineWidth = isSpotlight ? 2 : 1;
        ctx.stroke();
        if (isSpotlight) {
          ctx.beginPath();
          ctx.arc(x, y, 10, 0, Math.PI * 2);
          ctx.strokeStyle = "#ffffffa0";
          ctx.lineWidth = 1;
          ctx.stroke();
        }
      }
      ctx.beginPath();
      ctx.arc(ballX, ballY, 4.2, 0, Math.PI * 2);
      ctx.fillStyle = "#ffffff";
      ctx.shadowColor = "#ffffff";
      ctx.shadowBlur = 7;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
    drawSpotlightPath(left, right, top, bottom);
  }

  function drawSpotlightPath(left, right, top, bottom) {
    if (!state.spotlight || !state.selectedEvent) return;
    const player = state.spotlight;
    const related = state.events.filter((event) =>
      event.player === player && event.x != null && Math.abs(event.minute - state.selectedEvent.minute) <= 1
    );
    if (related.length < 2) return;
    const ctx = pitchContext;
    ctx.beginPath();
    related.forEach((event, index) => {
      const x = left + (Number(event.x) / 100) * (right - left);
      const y = top + (Number(event.y) / 100) * (bottom - top);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#ffffffb0";
    ctx.lineWidth = 2;
    ctx.setLineDash([4, 3]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function drawMomentum() {
    const { width, height } = resizeCanvas(chart, chartContext);
    const ctx = chartContext;
    const pad = { left: 7, right: 7, top: 8, bottom: 8 };
    const chartWidth = width - pad.left - pad.right;
    const chartHeight = height - pad.top - pad.bottom;
    const mid = pad.top + chartHeight / 2;
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = "#465148";
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(pad.left, mid);
    ctx.lineTo(width - pad.right, mid);
    ctx.stroke();
    ctx.setLineDash([]);
    if (!state.momentum.length) return;
    const points = state.momentum.map((item, index) => ({
      x: pad.left + (state.momentum.length === 1 ? chartWidth / 2 : (index / (state.momentum.length - 1)) * chartWidth),
      y: pad.top + ((1 - Math.max(-1, Math.min(1, Number(item.value)))) / 2) * chartHeight
    }));
    ctx.beginPath();
    points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#b6f35b";
    ctx.stroke();
    points.forEach((point) => {
      ctx.beginPath();
      ctx.arc(point.x, point.y, 2, 0, Math.PI * 2);
      ctx.fillStyle = "#d7ff9f";
      ctx.fill();
    });
  }

  function renderScore(data) {
    const score = data.score || {};
    $("#score").innerHTML = `${score.Rovers || 0} <span>—</span> ${score.United || 0}`;
    $("#minute").textContent = Number(data.minute || 0).toFixed(2);
    $("#match-status").textContent = String(data.status || "READY").toUpperCase();
    $("#source-label").textContent = `${String(data.source || "synthetic").toUpperCase()} DATA`;
    const latency = data.overlay_latency_ms;
    $("#latency-label").textContent = latency != null && Number.isFinite(Number(latency))
      ? `LATENCY ${Math.round(Number(latency))} MS`
      : "LATENCY —";
    const delay = Number(data.stream_delay_seconds);
    $("#delay-label").textContent = Number.isFinite(delay)
      ? `DELAY ${delay.toFixed(delay % 1 ? 1 : 0)} S`
      : "DELAY —";
    const average = data.agent_latency_avg_ms == null ? NaN : Number(data.agent_latency_avg_ms);
    const maximum = data.agent_latency_max_ms == null ? NaN : Number(data.agent_latency_max_ms);
    const agents = Object.values(data.agents || {});
    const working = agents.filter((agent) => agent.status === "working").length;
    $("#agent-latency-label").textContent =
      `AGENTS ${working}/4 · AVG ${Number.isFinite(average) ? Math.round(average) : "—"} / MAX ${Number.isFinite(maximum) ? Math.round(maximum) : "—"} MS`;
    $("#start-button").disabled = ["running", "starting", "live"].includes(data.status);
    $("#stop-button").disabled = !["running", "starting"].includes(data.status);
  }

  function renderEvents() {
    const log = $("#event-log");
    const events = state.events.slice(-45).reverse();
    if (!events.length) {
      log.innerHTML = '<p class="empty-note">Events will appear here as the match unfolds.</p>';
      return;
    }
    log.innerHTML = events.map((event) => {
      const key = `${event.minute}-${event.team}-${event.player}-${event.type}`;
      const selected = state.selectedEvent && state.selectedEvent.key === key ? " selected" : "";
      const symbol = event.outcome === "goal" ? "⚽" : event.type === "shot" ? "◉" : event.type === "tackle" ? "◇" : "·";
      const description = event.type === "shot" && event.outcome === "goal"
        ? `<strong>${escapeHtml(event.player)}</strong> scores`
        : `<strong>${escapeHtml(event.player)}</strong> ${escapeHtml(event.type)}${event.outcome ? ` · ${escapeHtml(event.outcome.replaceAll("_", " "))}` : ""}`;
      return `<button class="event-row${selected}" data-event-key="${escapeHtml(key)}" type="button">
        <span class="event-time">${Number(event.minute || 0).toFixed(1)}'</span>
        <span class="event-symbol ${event.team === "United" ? "united" : ""}">${symbol}</span>
        <span class="event-desc">${description}</span>
        <span class="event-tag">${escapeHtml(event.team || "")}</span>
      </button>`;
    }).join("");
    log.querySelectorAll(".event-row").forEach((row) => {
      row.addEventListener("click", () => {
        const event = events.find((item) => `${item.minute}-${item.team}-${item.player}-${item.type}` === row.dataset.eventKey);
        selectEvent(event);
      });
    });
  }

  function renderShape(shapeData) {
    const container = $("#shape-card");
    const teams = shapeData.teams || {};
    const content = Object.entries(teams).map(([team, value]) => {
      const formation = value.out_of_possession?.formation || "—";
      const block = value.block?.block || "—";
      const marking = value.marking?.style || "—";
      return `<div class="team-insight"><b>${escapeHtml(team)}</b><span>${escapeHtml(formation)} · ${escapeHtml(block)} · ${escapeHtml(marking)}</span></div>`;
    }).join("");
    container.innerHTML = content || '<p class="muted">Waiting for match data…</p>';
  }

  function renderPatterns(patternData) {
    const findings = patternData.findings || {};
    const container = $("#pattern-card");
    const entries = Object.entries(findings);
    if (!entries.length) {
      container.innerHTML = '<p class="muted">No flagged patterns in the current window.</p>';
      return;
    }
    container.innerHTML = entries.slice(0, 7).map(([name, finding]) => {
      let text = "";
      if (name.includes("flank")) text = `${finding.top_share ? Math.round(finding.top_share * 100) : 0}% pass/shot share · ${finding.top_flank}`;
      else if (name.includes("reliance")) text = `${escapeHtml(finding.top_player || "Player")} · ${Math.round(finding.share * 100)}% involvement`;
      else if (name.includes("turnover")) text = `${escapeHtml(finding.zone || "—")} · ${Math.round(finding.share * 100)}%`;
      else if (name.includes("press_trigger")) {
        const trigger = finding.best_trigger;
        text = `${escapeHtml(trigger || "—")} · ${Math.round((finding.triggers?.[trigger]?.rate || 0) * 100)}% press rate`;
      }
      return `<div class="pattern-line"><strong>${escapeHtml(name.replaceAll("_", " "))}</strong><br>${text}</div>`;
    }).join("");
  }

  function renderImpact(players) {
    const container = $("#impact-card");
    if (state.playerStatsError) {
      container.innerHTML = `<p class="muted">Player stats unavailable: ${escapeHtml(state.playerStatsError)}</p>`;
      return;
    }
    if (!players || !players.length) {
      container.innerHTML = '<p class="muted">Waiting for match data…</p>';
      return;
    }
    const ordered = [...players].sort((left, right) =>
      Number(right.impact_score || 0) - Number(left.impact_score || 0)
    );
    const metric = (value) => Number(value || 0).toFixed(1);
    container.innerHTML = `<div class="impact-table">
      <div class="impact-columns"><span>PLAYER</span><span>IMPACT</span><span>GRAVITY</span></div>
      ${ordered.map((player) => {
        const score = Math.max(0, Math.min(100, Number(player.impact_score || 0)));
        const components = player.impact_components || {};
        return `<div class="impact-entry">
          <div class="impact-row">
            <span class="impact-name"><b>${escapeHtml(player.name)}</b><small>${escapeHtml(player.team)}</small></span>
            <span class="impact-measure"><span class="impact-bar"><i style="width:${score}%"></i></span><b class="impact-score">${score.toFixed(1)}</b></span>
            <span class="impact-gravity">${metric(player.gravity_index)}</span>
          </div>
          <small class="impact-details">Components: INV ${metric(components.involvement)} · SHOT ${metric(components.shots_created)} · DEF ${metric(components.defensive_actions)} · MOM ${metric(components.momentum_while_on_ball)}<br>
            Gravity: near ${metric(player.gravity_with_ball)}/${metric(player.gravity_without_ball)} · pull ${metric(player.gravity_pull_with_ball)}/${metric(player.gravity_pull_without_ball)}
          </small>
        </div>`;
      }).join("")}
    </div>`;
  }

  async function refreshPlayerStats() {
    if (state.playerStatsRequest || Date.now() - state.lastPlayerStatsAt < 10000) return;
    state.lastPlayerStatsAt = Date.now();
    state.playerStatsRequest = api("/stats/players");
    try {
      const result = await state.playerStatsRequest;
      state.playerStats = result.players || [];
      state.playerStatsError = null;
      renderImpact(state.playerStats);
    } catch (error) {
      state.playerStatsError = error.message;
      renderImpact(state.playerStats);
      console.error("Could not load player stats", error);
    } finally {
      state.playerStatsRequest = null;
    }
  }

  function renderOverlay(overlay) {
    if (!overlay || typeof overlay !== "object") return;
    const overlayLatency = Number(overlay.latency_ms);
    if (Number.isFinite(overlayLatency)) {
      $("#latency-label").textContent = `LATENCY ${Math.round(overlayLatency)} MS`;
    }
    state.overlays = [
      overlay,
      ...state.overlays.filter((item) => item.overlay_id !== overlay.overlay_id)
    ].slice(0, 100);
    if (overlay.type === "narration") {
      $("#narration-text").textContent = overlay.body || "";
      $("#narration-minute").textContent = `${Number(overlay.match_clock / 60).toFixed(1)}'`;
      $("#fallback-badge").classList.toggle(
        "hidden",
        overlay.headline !== "Template fallback"
      );
    }
    const preview = $("#overlay-preview");
    preview.innerHTML = `<span class="overlay-type">${escapeHtml(overlay.type || "overlay")}</span>
      <strong>${escapeHtml(overlay.headline || "")}</strong>
      <span>${escapeHtml(overlay.body || "")}</span>
      <small>${escapeHtml(overlay.start_tc || "")} · ${escapeHtml(overlay.source_agent || "")}${Number.isFinite(overlayLatency) ? ` · ${Math.round(overlayLatency)} ms` : ""}</small>`;
    $("#overlay-json").textContent = JSON.stringify(overlay, null, 2);
  }

  function renderMoments(moments) {
    const container = $("#insight-popups");
    const latest = (moments || []).slice(-3);
    container.innerHTML = latest.map((moment) => `<button class="insight-chip" type="button" title="${escapeHtml(moment.why || "")}" data-moment-id="${escapeHtml(moment.id)}">${escapeHtml(String(moment.type || "insight").replaceAll("_", " "))} · ${Number(moment.minute || 0).toFixed(0)}'</button>`).join("");
    container.querySelectorAll(".insight-chip").forEach((button) => {
      button.addEventListener("click", () => {
        const moment = latest.find((item) => item.id === button.dataset.momentId);
        if (moment) replayMoment(moment);
      });
    });
  }

  function render(snapshot) {
    renderScore(snapshot);
    if (snapshot.events) state.events = snapshot.events;
    if (snapshot.frame !== undefined) state.frame = snapshot.frame;
    if (snapshot.shape) state.shape = snapshot.shape;
    if (snapshot.patterns) state.patterns = snapshot.patterns;
    if (snapshot.impact) state.impact = snapshot.impact;
    if (snapshot.momentum_series) state.momentum = snapshot.momentum_series;
    if (snapshot.moments) state.moments = snapshot.moments;
    renderShape(state.shape);
    renderPatterns(state.patterns);
    renderImpact(state.playerStats);
    void refreshPlayerStats();
    renderMoments(state.moments);
    renderEvents();
    drawPitch();
    drawMomentum();
    updatePlayerOptions(state.events);
    if (snapshot.error) showToast(snapshot.error);
  }

  function updatePlayerOptions(events) {
    const select = $("#favourite-player");
    const current = select.value;
    const players = [...new Set(events.map((event) => event.player).filter(Boolean))].sort();
    const options = ['<option value="">Any player</option>', ...players.map((player) =>
      `<option value="${escapeHtml(player)}">${escapeHtml(player)}</option>`
    )];
    select.innerHTML = options.join("");
    if (players.includes(current)) select.value = current;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  async function sendControl(action = null) {
    const payload = {
      speed: Number($("#speed").value),
      mode: $("#mode").value,
      language: $("#language").value,
      favourite_team: $("#favourite-team").value,
      favourite_player: $("#favourite-player").value || null
    };
    if (action) payload.action = action;
    const result = await api("/control", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    render(result);
    return result;
  }

  function selectEvent(event) {
    if (!event) return;
    state.selectedEvent = {
      ...event,
      key: `${event.minute}-${event.team}-${event.player}-${event.type}`
    };
    state.spotlight = event.player ? { player: event.player } : null;
    $("#replay-button").disabled = false;
    renderEvents();
    drawPitch();
    $("#spotlight-label").textContent = event.player ? `SPOTLIGHT · ${event.player.toUpperCase()}` : "NO PLAYER SPOTLIGHT";
    const timeParameter = event.t == null ? "" : `&t=${encodeURIComponent(event.t)}`;
    api(`/frame?minute=${encodeURIComponent(event.minute || 0)}${timeParameter}`)
      .then((result) => {
        if (state.selectedEvent?.key !== `${event.minute}-${event.team}-${event.player}-${event.type}`) return;
        state.frame = result.frame;
        drawPitch();
      })
      .catch((error) => {
        if (!/No replay frames/.test(error.message)) showToast(error.message);
      });
  }

  function replayMoment(moment) {
    const matchEvent = state.events.find((event) =>
      Math.abs(Number(event.minute) - Number(moment.minute)) < 0.1 &&
      (!moment.team || event.team === moment.team) &&
      (moment.type !== "goal" || event.outcome === "goal")
    );
    if (matchEvent) {
      selectEvent(matchEvent);
    } else {
      state.selectedEvent = {
        ...moment,
        minute: Number(moment.minute || 0),
        key: moment.id,
        player: moment.player || null
      };
      state.spotlight = moment.player ? { player: moment.player } : null;
      drawPitch();
      $("#spotlight-label").textContent = moment.player
        ? `SPOTLIGHT · ${moment.player.toUpperCase()}`
        : `MOMENT · ${String(moment.type || "").replaceAll("_", " ").toUpperCase()}`;
      api(`/frame?minute=${encodeURIComponent(moment.minute || 0)}`)
        .then((result) => {
          if (state.selectedEvent?.key !== moment.id) return;
          state.frame = result.frame;
          drawPitch();
        })
        .catch((error) => {
          if (!/No replay frames/.test(error.message)) showToast(error.message);
        });
    }
    showToast(moment.why || `Replaying ${moment.type || "match moment"}`);
  }

  function connectStream() {
    const stream = new EventSource(apiUrl("/stream"));
    stream.onopen = () => {
      $("#connection-label").textContent = "CONNECTED";
      $("#connection-label").previousElementSibling.style.background = "#b6f35b";
    };
    stream.onmessage = (event) => {
      try {
        render(JSON.parse(event.data));
      } catch (error) {
        console.error("Could not parse match stream update", error);
      }
    };
    stream.addEventListener("overlay", (event) => {
      try {
        renderOverlay(JSON.parse(event.data));
      } catch (error) {
        console.error("Could not parse overlay stream update", error);
      }
    });
    stream.onerror = () => {
      $("#connection-label").textContent = "RECONNECTING";
    };
  }

  $("#start-button").addEventListener("click", async () => {
    try { await sendControl("start"); } catch (error) { showToast(error.message); }
  });
  $("#stop-button").addEventListener("click", async () => {
    try { await sendControl("stop"); } catch (error) { showToast(error.message); }
  });
  for (const id of ["speed", "mode", "language", "favourite-team", "favourite-player"]) {
    $(`#${id}`).addEventListener("change", async () => {
      try { await sendControl(); } catch (error) { showToast(error.message); }
    });
  }
  $("#replay-button").addEventListener("click", () => {
    if (state.selectedEvent) {
      selectEvent(state.selectedEvent);
    }
  });
  window.addEventListener("resize", () => { drawPitch(); drawMomentum(); });

  connectStream();
  api("/overlays/recent?limit=100")
    .then((overlays) => {
      overlays.forEach(renderOverlay);
    })
    .catch((error) => showToast(error.message));
  api("/state").then(render).catch((error) => showToast(error.message));
})();
