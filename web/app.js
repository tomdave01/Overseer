(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const API_BASE_URL = String(window.OVERSEER_API_BASE_URL || "").replace(/\/+$/, "");
  const apiUrl = (path) => `${API_BASE_URL}${path}`;
  const canvas = $("#pitch");
  const ctx = canvas.getContext("2d");
  const COLORS = { Rovers: "#b7e276", United: "#e5a078" };
  const state = {
    status: "idle",
    score: { Rovers: 0, United: 0 },
    frame: null,
    previousFrame: null,
    frameReceivedAt: 0,
    frameBlendMs: 200,
    events: [],
    shape: {},
    patterns: {},
    momentum: [],
    players: [],
    teams: {},
    favouritePlayer: null,
    agents: {},
    overlays: [],
    commentary: [],
    selectedPlayer: null,
    mode: "Broadcast",
    language: "English",
    favouriteTeam: "Neutral",
    speed: 50,
    panel: null,
    historyOpen: false,
    agentStatus: null,
    lastAgentsAt: 0,
    lastPlayersAt: 0,
    lastTeamsAt: 0,
    lastHistoryAt: 0,
    request: {},
    recaps: {},
    recapLoading: new Set(),
    toastTimer: 0
  };
  const pinnedOverlays = new Set();
  const overlayExpiry = new Map();

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

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[char]);
  }

  function number(value, digits = 1) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(digits) : "—";
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("visible");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 3500);
  }

  function safeRequest(key, work, intervalMs = 0) {
    if (state.request[key]) return state.request[key];
    const lastKey = `last${key[0].toUpperCase()}${key.slice(1)}At`;
    if (intervalMs && Date.now() - (state[lastKey] || 0) < intervalMs) return null;
    state[lastKey] = Date.now();
    state.request[key] = work()
      .catch((error) => {
        showToast(error.message);
        throw error;
      })
      .finally(() => { state.request[key] = null; });
    return state.request[key];
  }

  function setConnection(status) {
    $("#connection-label").textContent = status;
    $("#connection-dot").className = status === "CONNECTED"
      ? "connected"
      : status === "RECONNECTING" ? "reconnecting" : "";
  }

  function applyPreferences(snapshot) {
    if (snapshot.mode) state.mode = snapshot.mode;
    if (snapshot.language) state.language = snapshot.language;
    if (snapshot.favourite_team) state.favouriteTeam = snapshot.favourite_team;
    if (snapshot.favourite_player !== undefined) state.favouritePlayer = snapshot.favourite_player;
    if (snapshot.speed) state.speed = Number(snapshot.speed);
    $("#mode").value = state.mode;
    $("#language").value = state.language;
    $("#favourite-team").value = state.favouriteTeam;
    $("#speed").value = String(state.speed);
    document.documentElement.lang = state.language === "Arabic" ? "ar" : state.language === "Spanish" ? "es" : state.language === "French" ? "fr" : "en";
    document.documentElement.dir = state.language === "Arabic" ? "rtl" : "ltr";
  }

  function renderScore(snapshot) {
    const score = snapshot.score || state.score;
    state.score = score;
    $("#score").innerHTML = `${Number(score.Rovers || 0)} <i>—</i> ${Number(score.United || 0)}`;
    $("#minute").textContent = number(snapshot.minute, 2);
    state.status = snapshot.status || state.status;
    $("#match-status").textContent = String(state.status).replaceAll("_", " ").toUpperCase();
    $("#source-label").textContent = `${String(snapshot.source || "synthetic").toUpperCase()} DATA`;
    $("#delay-label").textContent = `DISPLAY DELAY ${number(snapshot.stream_delay_seconds, 0)} S`;
    $("#start-button").disabled = ["running", "starting", "live"].includes(state.status);
    $("#stop-button").disabled = !["running", "starting"].includes(state.status);
    const recapReady = Number(snapshot.minute || 0) >= 45 || state.status === "complete";
    $("#recap-nav").disabled = !recapReady;
  }

  function canvasSize() {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.max(1, Math.round(rect.width * ratio));
    const height = Math.max(1, Math.round(rect.height * ratio));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { width: rect.width, height: rect.height };
  }

  function blendedFrame(now) {
    if (!state.frame) return null;
    const from = state.previousFrame || state.frame;
    const to = state.frame;
    const alpha = Math.min(1, Math.max(0, (now - state.frameReceivedAt) / state.frameBlendMs));
    const oldById = new Map((from.players || []).map((player) => [player.id, player]));
    const players = (to.players || []).map((player) => {
      const old = oldById.get(player.id);
      if (!old || alpha >= 1) return player;
      return {
        ...player,
        x: Number(old.x) + (Number(player.x) - Number(old.x)) * alpha,
        y: Number(old.y) + (Number(player.y) - Number(old.y)) * alpha
      };
    });
    const oldBall = from.ball || to.ball || {};
    const newBall = to.ball || oldBall;
    return {
      ...to,
      players,
      ball: {
        x: Number(oldBall.x || 0) + (Number(newBall.x || 0) - Number(oldBall.x || 0)) * alpha,
        y: Number(oldBall.y || 0) + (Number(newBall.y || 0) - Number(oldBall.y || 0)) * alpha
      }
    };
  }

  function drawPitch() {
    const { width, height } = canvasSize();
    if (!width || !height) return;
    const pad = 12;
    const left = pad, top = pad, right = width - pad, bottom = height - pad;
    const w = right - left, h = bottom - top;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#294b3b";
    ctx.fillRect(left, top, w, h);
    for (let stripe = 0; stripe < 10; stripe += 1) {
      ctx.fillStyle = stripe % 2 ? "rgba(187,218,191,.025)" : "rgba(5,20,13,.09)";
      ctx.fillRect(left + stripe * w / 10, top, w / 10, h);
    }
    ctx.strokeStyle = "rgba(226,241,226,.78)";
    ctx.lineWidth = Math.max(1, Math.min(1.6, width / 1000));
    ctx.strokeRect(left, top, w, h);
    ctx.beginPath();
    ctx.moveTo(left + w / 2, top);
    ctx.lineTo(left + w / 2, bottom);
    ctx.stroke();
    const centerX = left + w / 2, centerY = top + h / 2;
    ctx.beginPath();
    ctx.arc(centerX, centerY, h * 0.145, 0, Math.PI * 2);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(centerX, centerY, 2.2, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(238,247,237,.8)";
    ctx.fill();
    const penaltyH = h * 0.60, penaltyW = w * 0.165;
    const goalH = h * 0.28, goalW = w * 0.055;
    const penaltyY = centerY - penaltyH / 2;
    ctx.strokeRect(left, penaltyY, penaltyW, penaltyH);
    ctx.strokeRect(right - penaltyW, penaltyY, penaltyW, penaltyH);
    ctx.strokeRect(left, centerY - goalH / 2, goalW, goalH);
    ctx.strokeRect(right - goalW, centerY - goalH / 2, goalW, goalH);
    ctx.beginPath();
    ctx.arc(left + w * 0.11, centerY, 2, 0, Math.PI * 2);
    ctx.arc(right - w * 0.11, centerY, 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(left + w * 0.11, centerY, h * 0.10, -0.88, 0.88);
    ctx.arc(right - w * 0.11, centerY, h * 0.10, Math.PI - 0.88, Math.PI + 0.88);
    ctx.stroke();
    const frame = blendedFrame(performance.now());
    if (frame) {
      $("#pitch-empty").hidden = true;
      $("#possession-label").textContent = `POSSESSION ${String(frame.poss || "—").toUpperCase()}`;
      if ($("#toggle-heat").checked) drawHeat(frame, left, top, w, h);
      if ($("#toggle-passes").checked) drawPasses(left, top, w, h);
      if ($("#toggle-shape").checked) drawTeamShapes(frame, left, top, w, h);
      drawPlayers(frame, left, top, w, h);
      drawBall(frame.ball || {}, left, top, w, h);
    } else {
      $("#pitch-empty").hidden = false;
    }
  }

  function animatePitch() {
    drawPitch();
    requestAnimationFrame(animatePitch);
  }

  function drawHeat(frame, left, top, width, height) {
    const events = state.events.filter((event) => Number.isFinite(Number(event.x))).slice(-18);
    const centers = events.length
      ? events.map((event) => ({ x: Number(event.x), y: Number(event.y), team: event.team }))
      : frame.ball ? [{ ...frame.ball, team: frame.poss }] : [];
    for (const point of centers) {
      const x = left + point.x / 100 * width, y = top + point.y / 100 * height;
      const gradient = ctx.createRadialGradient(x, y, 0, x, y, height * 0.12);
      const color = point.team === "United" ? "229,160,120" : "183,226,118";
      gradient.addColorStop(0, `rgba(${color},.14)`);
      gradient.addColorStop(1, `rgba(${color},0)`);
      ctx.fillStyle = gradient;
      ctx.fillRect(x - height * 0.12, y - height * 0.12, height * 0.24, height * 0.24);
    }
  }

  function drawPasses(left, top, width, height) {
    for (const event of state.events.slice(-40)) {
      if (event.type !== "pass" || event.end_x == null) continue;
      ctx.beginPath();
      ctx.moveTo(left + Number(event.x) / 100 * width, top + Number(event.y) / 100 * height);
      ctx.lineTo(left + Number(event.end_x) / 100 * width, top + Number(event.end_y) / 100 * height);
      ctx.strokeStyle = event.team === "United" ? "rgba(229,160,120,.5)" : "rgba(183,226,118,.5)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 5]);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  function drawTeamShapes(frame, left, top, width, height) {
    for (const team of ["Rovers", "United"]) {
      const players = (frame.players || []).filter((player) => player.team === team && player.role !== "GK");
      if (players.length < 3) continue;
      const center = players.reduce((sum, player) => ({
        x: sum.x + Number(player.x) / players.length,
        y: sum.y + Number(player.y) / players.length
      }), { x: 0, y: 0 });
      ctx.beginPath();
      ctx.moveTo(left + center.x / 100 * width, top + center.y / 100 * height);
      for (const player of players) ctx.lineTo(left + Number(player.x) / 100 * width, top + Number(player.y) / 100 * height);
      ctx.closePath();
      ctx.strokeStyle = team === "Rovers" ? "rgba(183,226,118,.42)" : "rgba(229,160,120,.42)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 5]);
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }

  function drawPlayers(frame, left, top, width, height) {
    for (const player of frame.players || []) {
      const x = left + Number(player.x) / 100 * width;
      const y = top + Number(player.y) / 100 * height;
      const selected = state.selectedPlayer?.id === player.id;
      const radius = Math.max(8, Math.min(13, height * 0.019));
      ctx.beginPath();
      ctx.arc(x, y, selected ? radius + 3 : radius, 0, Math.PI * 2);
      ctx.fillStyle = selected ? "#f5f8ee" : COLORS[player.team] || "#dce5dc";
      ctx.fill();
      ctx.strokeStyle = selected ? COLORS[player.team] : "rgba(12,21,17,.92)";
      ctx.lineWidth = selected ? 2.4 : 1.5;
      ctx.stroke();
      ctx.fillStyle = selected ? "#111a15" : "#18221d";
      ctx.font = `700 ${Math.max(7, radius * 0.85)}px "Segoe UI", sans-serif`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(player.shirt ?? player.number ?? "·"), x, y + .3);
    }
  }

  function drawBall(ball, left, top, width, height) {
    const x = left + Number(ball.x || 0) / 100 * width;
    const y = top + Number(ball.y || 0) / 100 * height;
    ctx.beginPath();
    ctx.arc(x, y, 4.2, 0, Math.PI * 2);
    ctx.fillStyle = "#fff";
    ctx.shadowColor = "#fff";
    ctx.shadowBlur = 9;
    ctx.fill();
    ctx.shadowBlur = 0;
  }

  function renderSnapshot(snapshot) {
    renderScore(snapshot);
    applyPreferences(snapshot);
    if (snapshot.frame !== undefined && snapshot.frame) {
      state.previousFrame = state.frame || snapshot.frame;
      state.frame = snapshot.frame;
      state.frameReceivedAt = performance.now();
    }
    if (snapshot.events) state.events = snapshot.events;
    if (snapshot.shape) state.shape = snapshot.shape;
    if (snapshot.patterns) state.patterns = snapshot.patterns;
    if (snapshot.momentum_series) state.momentum = snapshot.momentum_series;
    if (snapshot.impact) state.impact = snapshot.impact;
    if (snapshot.agents) state.agents = snapshot.agents;
    renderPlayerCard();
    refreshDataForOpenPanel();
  }

  function renderOverlay(overlay) {
    if (!overlay || typeof overlay !== "object" || typeof overlay.overlay_id !== "string") return;
    state.overlays = [overlay, ...state.overlays.filter((item) => item.overlay_id !== overlay.overlay_id)].slice(0, 100);
    if (overlay.type === "narration") {
      state.commentary = [overlay, ...state.commentary.filter((item) => item.overlay_id !== overlay.overlay_id)].slice(0, 500);
      renderLatestCommentary();
      if (state.historyOpen) renderHistory();
    }
    if (!state.overlays.some((item) => item.overlay_id === overlay.overlay_id)) return;
    if (!document.querySelector(`[data-insight-id="${CSS.escape(overlay.overlay_id)}"]`)) {
      const timeout = Math.max(250, Number(overlay.duration_ms) || 8000);
      overlayExpiry.set(overlay.overlay_id, Date.now() + timeout);
      window.setTimeout(() => {
        if (pinnedOverlays.has(overlay.overlay_id)) return;
        overlayExpiry.delete(overlay.overlay_id);
        state.overlays = state.overlays.filter((item) => item.overlay_id !== overlay.overlay_id);
        renderInsights();
      }, timeout);
    }
    renderInsights();
  }

  function insightText(overlay) {
    if (state.mode === "Analyst" && overlay.stat) {
      return `${overlay.body || ""}${overlay.body ? " · " : ""}${overlay.stat.label}: ${overlay.stat.value}`;
    }
    return overlay.body || "";
  }

  function renderInsights() {
    const stack = $("#insight-stack");
    const active = state.overlays
      .filter((overlay) => pinnedOverlays.has(overlay.overlay_id) || !overlayExpiry.has(overlay.overlay_id) || overlayExpiry.get(overlay.overlay_id) > Date.now())
      .sort((a, b) => Number(b.priority || 0) - Number(a.priority || 0) || Number(b.seq || 0) - Number(a.seq || 0))
      .slice(0, 5);
    stack.innerHTML = active.map((overlay) => {
      const priority = Number(overlay.priority || 0);
      const importance = priority >= 70 ? "HIGH PRIORITY" : priority >= 40 ? "IMPORTANT" : "UPDATE";
      return `<article class="insight-card ${priority >= 70 ? "priority-high" : ""} ${pinnedOverlays.has(overlay.overlay_id) ? "pinned" : ""}" data-insight-id="${escapeHtml(overlay.overlay_id)}" style="--card-priority:${priority}">
        <button class="insight-pin" type="button" aria-label="${pinnedOverlays.has(overlay.overlay_id) ? "Unpin" : "Pin"} insight" data-action="pin">${pinnedOverlays.has(overlay.overlay_id) ? "◆" : "◇"}</button>
        <button class="insight-dismiss" type="button" aria-label="Dismiss insight" data-action="dismiss">×</button>
        <div class="insight-meta"><span>${escapeHtml(importance)}</span><time>${escapeHtml(overlay.start_tc || "")}</time></div>
        <strong>${escapeHtml(overlay.headline || overlay.type || "Match update")}</strong>
        <p>${escapeHtml(insightText(overlay))}</p>
        ${overlay.stat && state.mode !== "Analyst" ? `<small>${escapeHtml(overlay.stat.label)} · ${escapeHtml(overlay.stat.value)}</small>` : ""}
      </article>`;
    }).join("");
  }

  function renderLatestCommentary() {
    const latest = state.commentary[0];
    $("#commentary-minute").textContent = latest ? `${number(Number(latest.match_clock || 0) / 60, 1)}′` : "—";
    $("#commentary-text").textContent = latest
      ? (latest.body || latest.headline || "Match commentary")
      : "Commentary will appear here as the match unfolds.";
    $("#commentary-importance").textContent = latest
      ? Number(latest.priority || 0) >= 70 ? "KEY MOMENT" : "COMMENTARY"
      : "LIVE";
    $("#commentary-importance").classList.toggle("high", Boolean(latest && Number(latest.priority || 0) >= 70));
  }

  function renderHistory() {
    const list = $("#history-list");
    const importance = $("#history-importance").value;
    const playerTerm = $("#history-player").value.trim().toLocaleLowerCase();
    const items = [...state.commentary].reverse().filter((item) => {
      const high = Number(item.priority || 0) >= 70;
      const importanceMatches = importance === "all" || (importance === "high" ? high : !high);
      const playerMatches = !playerTerm || String(item.player || "").toLocaleLowerCase().includes(playerTerm);
      return importanceMatches && playerMatches;
    });
    if (!items.length) {
      list.innerHTML = '<p class="empty-note">No commentary matches these filters.</p>';
      return;
    }
    list.innerHTML = items.map((item) => `<article class="history-entry">
      <div class="history-entry-meta"><time>${number(Number(item.match_clock || 0) / 60, 1)}′</time><span>${Number(item.priority || 0) >= 70 ? "KEY MOMENT" : "COMMENTARY"}</span></div>
      <strong>${escapeHtml(item.headline || "Match commentary")}</strong>
      <p>${escapeHtml(item.body || "")}</p>
      ${item.player ? `<small>${escapeHtml(item.player)} · ${escapeHtml(item.team || "")}</small>` : ""}
    </article>`).join("");
  }

  async function loadHistory() {
    if (Date.now() - state.lastHistoryAt < 5000 && state.commentary.length) return;
    state.lastHistoryAt = Date.now();
    try {
      const items = await api("/commentary?limit=500");
      const ids = new Set(state.commentary.map((item) => item.overlay_id));
      state.commentary = [...items.filter((item) => !ids.has(item.overlay_id)), ...state.commentary]
        .sort((left, right) => Number(right.match_clock || 0) - Number(left.match_clock || 0))
        .slice(0, 500);
      renderLatestCommentary();
      renderHistory();
    } catch (error) {
      showToast(error.message);
    }
  }

  function setHistory(open) {
    state.historyOpen = open;
    $("#history-panel").classList.toggle("open", open);
    $("#history-panel").setAttribute("aria-hidden", String(!open));
    $("#history-panel").inert = !open;
    $("#panel-scrim").hidden = !open && !state.panel;
    if (open) {
      void loadHistory();
      $("#history-close").focus();
    }
  }

  function activatePanel(name) {
    if (state.panel === name) {
      closePanel();
      return;
    }
    state.panel = name;
    state.historyOpen = false;
    $("#history-panel").classList.remove("open");
    $("#history-panel").setAttribute("aria-hidden", "true");
    $("#history-panel").inert = true;
    $("#data-panel").classList.add("open");
    $("#data-panel").setAttribute("aria-hidden", "false");
    $("#data-panel").inert = false;
    $("#panel-scrim").hidden = false;
    const titles = {
      teams: "Team stats", players: "Player stats", impact: "Player impact",
      shape: "Shape & patterns", momentum: "Match momentum", agents: "Agents",
      recap: "Match recap", raw: "Raw overlay JSON"
    };
    $("#panel-title").textContent = titles[name] || "Match data";
    refreshDataForOpenPanel(true);
    $("#panel-close").focus();
  }

  function closePanel() {
    state.panel = null;
    $("#data-panel").classList.remove("open");
    $("#data-panel").setAttribute("aria-hidden", "true");
    $("#data-panel").inert = true;
    $("#panel-scrim").hidden = !state.historyOpen;
  }

  function refreshDataForOpenPanel(force = false) {
    if (!state.panel) return;
    if (state.panel === "players" || state.panel === "impact") void loadPlayers(force);
    else if (state.panel === "teams") void loadTeams(force);
    else if (state.panel === "agents") void loadAgents(force);
    else renderPanel();
  }

  async function loadPlayers(force = false) {
    if (!force && Date.now() - state.lastPlayersAt < 10000) return renderPanel();
    const work = safeRequest("players", async () => {
      const result = await api("/stats/players");
      state.players = result.players || [];
      state.lastPlayersAt = Date.now();
      renderPanel();
    }, force ? 0 : 10000);
    if (work) await work.catch(() => {});
  }

  async function loadTeams(force = false) {
    if (!force && Date.now() - state.lastTeamsAt < 10000) return renderPanel();
    const work = safeRequest("teams", async () => {
      const result = await api("/stats/teams");
      state.teams = result.teams || {};
      state.lastTeamsAt = Date.now();
      renderPanel();
    }, force ? 0 : 10000);
    if (work) await work.catch(() => {});
  }

  async function loadAgents(force = false) {
    if (!force && Date.now() - state.lastAgentsAt < 5000) return renderPanel();
    try {
      state.agentStatus = await api("/agents/status");
      state.lastAgentsAt = Date.now();
      renderPanel();
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderPanel() {
    if (!state.panel) return;
    const content = $("#panel-content");
    const source = state.panel;
    if (source === "teams") renderTeamPanel(content);
    else if (source === "players") renderPlayerTable(content);
    else if (source === "impact") renderImpactPanel(content);
    else if (source === "shape") renderShapePanel(content);
    else if (source === "momentum") renderMomentumPanel(content);
    else if (source === "agents") renderAgentsPanel(content);
    else if (source === "recap") renderRecapPanel(content);
    else if (source === "raw") renderRawPanel(content);
  }

  function renderTeamPanel(content) {
    const entries = Object.values(state.teams || {});
    if (!entries.length) {
      content.innerHTML = '<p class="panel-empty">Team statistics are loading.</p>';
      return;
    }
    const metrics = [
      ["Possession", "possession_pct", "%"],
      ["Pass accuracy", "pass_accuracy_pct", "%"],
      ["Passes attempted", "passes_attempted", ""],
      ["Passes completed", "passes_completed", ""],
      ["Shots on target", "shots_on_target", ""],
      ["Goals", "goals", ""],
      ["Pressures", "pressures", ""],
      ["Turnovers", "turnovers", ""],
      ["Momentum", "momentum", ""]
    ];
    content.innerHTML = `<div class="team-comparison">${entries.map((team) => `<article class="team-stat-card">
      <header><span class="team-swatch ${team.team === "United" ? "united" : ""}"></span><h2>${escapeHtml(team.team)}</h2></header>
      ${metrics.map(([label, key, suffix]) => {
        const value = Number(team[key] || 0);
        const max = key.includes("pct") ? 100 : Math.max(1, ...entries.map((entry) => Number(entry[key] || 0)));
        return `<div class="team-metric"><div><span>${label}</span><b>${number(value, key.includes("pct") ? 1 : 0)}${suffix}</b></div><i><em class="${team.team === "United" ? "united" : ""}" style="width:${Math.min(100, value / max * 100)}%"></em></i></div>`;
      }).join("")}
    </article>`).join("")}</div>`;
  }

  const PLAYER_COLUMNS = [
    ["Player", "name", "text"], ["Club", "team", "text"], ["Role", "role", "text"],
    ["Shirt", "shirt", "number"], ["Touches", "touches", "number"],
    ["Passes", "passes_attempted", "number"], ["Completed", "passes_completed", "number"],
    ["Pass %", "pass_accuracy_pct", "number"], ["Shots", "shots", "number"],
    ["On target", "shots_on_target", "number"], ["Goals", "goals", "number"],
    ["Tackles won", "tackles_won", "number"], ["Tackles lost", "tackles_lost", "number"],
    ["Pressures", "pressures", "number"], ["Fouls", "fouls", "number"],
    ["Turnovers", "turnovers_lost", "number"], ["Distance m", "distance_covered_metres", "number"],
    ["Top speed", "top_speed_kmh", "number"], ["Minutes", "minutes_on_pitch", "number"],
    ["Influence", "impact_score", "number"], ["Gravity", "gravity_index", "number"],
    ["Near on", "gravity_with_ball", "number"], ["Near off", "gravity_without_ball", "number"],
    ["Pull on", "gravity_pull_with_ball", "number"], ["Pull off", "gravity_pull_without_ball", "number"]
  ];

  function sortPlayers(players, field) {
    return [...players].sort((a, b) => {
      const left = a[field], right = b[field];
      if (typeof left === "number" || typeof right === "number") return Number(right || 0) - Number(left || 0);
      return String(left || "").localeCompare(String(right || ""));
    });
  }

  function playerRows(players, fields) {
    return players.map((player) => `<tr data-player-id="${escapeHtml(player.id || `${player.team}-${player.name}`)}">
      ${fields.map((field) => `<td>${field === "team" ? `<span class="team-tag ${player.team === "United" ? "united" : ""}">${escapeHtml(player.team)}</span>` : escapeHtml(typeof player[field] === "number" ? number(player[field], field === "shirt" || field === "goals" || field === "shots" || field === "tackles_won" || field === "pressures" ? 0 : 1) : player[field] ?? "—")}</td>`).join("")}
    </tr>`).join("");
  }

  function renderPlayerTable(content) {
    const filter = $("#team-filter")?.value || "all";
    const sort = $("#player-sort")?.value || "impact_score";
    const players = sortPlayers(state.players.filter((player) => filter === "all" || player.team === filter), sort);
    content.innerHTML = `<div class="panel-toolbar">
      <label>TEAM <select id="team-filter"><option value="all">All players</option><option value="Rovers" ${filter === "Rovers" ? "selected" : ""}>Rovers</option><option value="United" ${filter === "United" ? "selected" : ""}>United</option></select></label>
      <label>SORT BY <select id="player-sort">${PLAYER_COLUMNS.filter((column) => column[2] === "number" || column[1] === "name").map((column) => `<option value="${column[1]}" ${sort === column[1] ? "selected" : ""}>${column[0]}</option>`).join("")}</select></label>
      <span class="table-count">${players.length} / 22 PLAYERS</span>
    </div>
    ${players.length ? `<div class="table-wrap"><table class="data-table player-data-table"><thead><tr>${PLAYER_COLUMNS.map((column) => `<th scope="col">${column[0]}</th>`).join("")}</tr></thead><tbody>${playerRows(players, PLAYER_COLUMNS.map((column) => column[1]))}</tbody></table></div>` : '<p class="panel-empty">No players match this filter.</p>'}`;
    $("#team-filter")?.addEventListener("change", renderPanel);
    $("#player-sort")?.addEventListener("change", renderPanel);
    content.querySelectorAll("tbody tr").forEach((row) => {
      row.addEventListener("click", () => {
        const player = players.find((item) => `${item.team}-${item.name}` === row.dataset.playerId);
        if (player) selectPlayer(player);
      });
    });
  }

  function renderImpactPanel(content) {
    if (!state.players.length) {
      content.innerHTML = '<p class="panel-empty">Player impact is loading.</p>';
      return;
    }
    const players = [...state.players].sort((a, b) => Number(b.impact_score || 0) - Number(a.impact_score || 0));
    const components = [
      ["Involvement", "involvement"], ["Shots created", "shots_created"],
      ["Defensive actions", "defensive_actions"], ["Momentum on ball", "momentum_while_on_ball"]
    ];
    content.innerHTML = `<p class="panel-intro">All 22 players ranked by measured influence. Component bars show each contribution.</p>
      <div class="impact-list">${players.map((player, index) => {
        const score = Math.max(0, Math.min(100, Number(player.impact_score || 0)));
        return `<article class="impact-player">
          <span class="impact-rank">${String(index + 1).padStart(2, "0")}</span>
          <div class="impact-player-main"><div class="impact-player-heading"><strong>${escapeHtml(player.name)}</strong><span>${escapeHtml(player.team)} · #${escapeHtml(player.shirt ?? "—")}</span><b>${number(score, 1)}</b></div>
            <div class="impact-track"><i style="width:${score}%"></i></div>
            <div class="component-grid">${components.map(([label, key]) => {
              const value = Number(player.impact_components?.[key] || 0);
              return `<div class="component"><span>${label}</span><i><em style="width:${Math.min(100, Math.max(0, value))}%"></em></i><b>${number(value, 1)}</b></div>`;
            }).join("")}</div>
            <small class="gravity-line">GRAVITY ${number(player.gravity_index, 1)} · NEAR ${number(player.gravity_with_ball, 1)} / ${number(player.gravity_without_ball, 1)} · PULL ${number(player.gravity_pull_with_ball, 1)} / ${number(player.gravity_pull_without_ball, 1)}</small>
          </div>
        </article>`;
      }).join("")}</div>`;
  }

  function renderShapePanel(content) {
    const teams = state.shape.teams || {};
    const patterns = state.patterns.findings || {};
    const teamCards = Object.entries(teams).map(([name, data]) => `<article class="shape-team">
      <h2><i class="team-swatch ${name === "United" ? "united" : ""}"></i>${escapeHtml(name)}</h2>
      <dl><dt>Formation</dt><dd>${escapeHtml(data.out_of_possession?.formation || "—")}</dd><dt>Block</dt><dd>${escapeHtml(data.block?.block || "—")}</dd><dt>Marking</dt><dd>${escapeHtml(data.marking?.style || "—")}</dd><dt>Line height</dt><dd>${number(data.out_of_possession?.line_height, 1)}</dd></dl>
    </article>`).join("");
    const patternRows = Object.entries(patterns).map(([name, value]) => `<article class="finding-row"><b>${escapeHtml(name.replaceAll("_", " "))}</b><span>${escapeHtml(value.top_player || value.zone || value.best_trigger || value.top_flank || "Flagged trend")}</span><small>${escapeHtml(JSON.stringify(value))}</small></article>`).join("");
    content.innerHTML = `<div class="shape-grid">${teamCards || '<p class="panel-empty">Shape analysis is waiting for tracking data.</p>'}</div>
      <h2 class="panel-subtitle">Flagged patterns</h2>${patternRows || '<p class="panel-empty">No significant patterns in the current window.</p>'}`;
  }

  function renderMomentumPanel(content) {
    const points = state.momentum || [];
    if (!points.length) {
      content.innerHTML = '<p class="panel-empty">Momentum will appear as match events are analysed.</p>';
      return;
    }
    const rows = [...points].reverse().slice(0, 30).map((point) => `<div class="momentum-row">
      <time>${number(point.minute, 1)}′</time><span class="${Number(point.value) >= 0 ? "rovers-text" : "united-text"}">${Number(point.value) >= 0 ? "ROVERS" : "UNITED"}</span><i><em style="width:${Math.min(100, Math.abs(Number(point.value || 0)) * 100)}%"></em></i><b>${number(Math.abs(Number(point.value || 0)) * 100, 0)}</b>
    </div>`).join("");
    content.innerHTML = `<div class="momentum-legend"><span>← United advantage</span><span>Rovers advantage →</span></div><div class="momentum-list">${rows}</div>`;
  }

  function renderAgentsPanel(content) {
    const result = state.agentStatus;
    if (!result) {
      content.innerHTML = '<p class="panel-empty">Agent status is loading.</p>';
      return;
    }
    const agents = result.agents || {};
    content.innerHTML = `<div class="agents-summary"><span>DISPLAY DELAY<strong>${number(result.stream_delay_seconds, 1)} s</strong></span><span>AVG LATENCY<strong>${result.average_latency_ms == null ? "—" : `${Math.round(result.average_latency_ms)} ms`}</strong></span><span>MAX LATENCY<strong>${result.max_latency_ms == null ? "—" : `${Math.round(result.max_latency_ms)} ms`}</strong></span></div>
      <div class="agent-list">${Object.entries(agents).map(([name, agent]) => `<article class="agent-status-card">
        <span class="agent-status-dot ${agent.status === "working" ? "working" : agent.status === "complete" ? "complete" : ""}"></span>
        <div><strong>${escapeHtml(name.replaceAll(/([a-z])([A-Z])/g, "$1 $2"))}</strong><small>${escapeHtml(agentTaskLabel(agent))}</small></div>
        <b>${agent.latency_ms == null ? "—" : `${Math.round(agent.latency_ms)} ms`}</b>
      </article>`).join("")}</div>`;
  }

  function agentTaskLabel(agent) {
    const task = agent.task;
    if (typeof task === "string") return task;
    if (task?.window) {
      return `Analysis · ${number(task.window.start_minute, 1)}–${number(task.window.end_minute, 1)}′`;
    }
    return String(agent.status || "idle");
  }

  async function renderRecapPanel(content) {
    const recapType = state.status === "complete" ? "full_time" : "halftime";
    content.innerHTML = `<div class="recap-tabs"><button type="button" data-recap-type="halftime" class="${recapType === "halftime" ? "active" : ""}">HALF-TIME</button><button type="button" data-recap-type="full_time" class="${recapType === "full_time" ? "active" : ""}">FULL-TIME</button></div><p class="panel-empty">Loading match recap…</p>`;
    content.querySelectorAll("[data-recap-type]").forEach((button) => {
      button.addEventListener("click", () => loadRecap(content, button.dataset.recapType));
    });
    if (state.recaps[recapType]) renderRecapResult(content, state.recaps[recapType], recapType);
    else if (!state.recapLoading.has(recapType)) await loadRecap(content, recapType);
  }

  async function loadRecap(content, recapType) {
    if (state.recapLoading.has(recapType)) return;
    state.recapLoading.add(recapType);
    content.querySelector(".panel-empty")?.replaceChildren(document.createTextNode("Loading match recap…"));
    try {
      const result = await api(`/recap?recap_type=${encodeURIComponent(recapType)}`);
      state.recaps[recapType] = result;
      renderRecapResult(content, result, recapType);
    } catch (error) {
      const empty = content.querySelector(".panel-empty");
      if (empty) empty.textContent = `Recap unavailable: ${error.message}`;
      else content.insertAdjacentHTML("beforeend", `<p class="panel-empty">Recap unavailable: ${escapeHtml(error.message)}</p>`);
    } finally {
      state.recapLoading.delete(recapType);
    }
  }

  function renderRecapResult(content, result, recapType) {
    content.querySelector(".panel-empty")?.remove();
    let output = content.querySelector(".recap-result");
    if (!output) {
      output = document.createElement("article");
      output.className = "recap-result";
      content.append(output);
    }
    output.innerHTML = `<span class="panel-kicker">${escapeHtml(result.recap_type.replaceAll("_", " ").toUpperCase())} · ${escapeHtml(result.language)} · ${escapeHtml(result.mode)}</span><p>${escapeHtml(result.text)}</p><span class="recap-score">ROVERS ${result.facts.score.Rovers} — ${result.facts.score.United} UNITED</span>${result.fallback ? '<small class="fallback-note">Deterministic fallback narration</small>' : ""}`;
    content.querySelectorAll("[data-recap-type]").forEach((button) => button.classList.toggle("active", button.dataset.recapType === recapType));
  }

  function renderRawPanel(content) {
    if (!state.overlays.length) {
      content.innerHTML = '<p class="panel-empty">No overlay payloads have been received yet.</p>';
      return;
    }
    content.innerHTML = `<p class="panel-intro">Latest overlay payloads, newest first. This view is hidden until opened.</p>
      <div class="raw-list">${state.overlays.map((overlay) => `<details class="raw-entry"><summary>${escapeHtml(overlay.type)} · ${escapeHtml(overlay.headline)} <time>${escapeHtml(overlay.start_tc || "")}</time></summary><pre>${escapeHtml(JSON.stringify(overlay, null, 2))}</pre></details>`).join("")}</div>`;
  }

  function renderPlayerCard() {
    const card = $("#player-card");
    const selected = state.selectedPlayer;
    if (!selected) {
      card.hidden = true;
      $("#spotlight-label").textContent = "NO PLAYER SELECTED";
      return;
    }
    const player = state.players.find((item) => item.name === selected.name && item.team === selected.team) || selected;
    card.hidden = false;
    $("#player-card-team").textContent = `${String(player.team || "").toUpperCase()} · PLAYER PROFILE`;
    $("#player-card-name").textContent = player.name || selected.name;
    $("#player-card-role").textContent = `${player.role || selected.role || "PLAYER"} · SHIRT ${player.shirt ?? selected.shirt ?? "—"}`;
    $("#player-card-stats").innerHTML = [
      ["Goals", player.goals], ["Pass %", `${number(player.pass_accuracy_pct, 0)}%`],
      ["Influence", number(player.impact_score, 1)], ["Gravity", number(player.gravity_index, 1)],
      ["Near · on", number(player.gravity_with_ball, 1)], ["Pull · on", number(player.gravity_pull_with_ball, 1)]
    ].map(([label, value]) => `<span><small>${label}</small><b>${value ?? "0"}</b></span>`).join("");
    $("#spotlight-label").textContent = `FOCUS · ${String(player.name || selected.name).toUpperCase()}`;
    $("#follow-player").textContent = state.mode === "Fan" && state.favouritePlayer === player.name
      ? "Following in Fan mode" : "Follow in Fan mode";
  }

  function selectPlayer(player) {
    const selected = state.players.find((item) => item.name === player.name && item.team === player.team) || player;
    state.selectedPlayer = selected;
    renderPlayerCard();
    void loadPlayers().then(renderPlayerCard);
    if (state.mode === "Fan") {
      void sendControl(null, selected.name).catch((error) => showToast(error.message));
    }
  }

  function hitTestPlayer(event) {
    const frame = blendedFrame(performance.now());
    if (!frame) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left, y = event.clientY - rect.top;
    const pad = 12, pitchW = rect.width - pad * 2, pitchH = rect.height - pad * 2;
    let nearest = null, nearestDistance = 20 * 20;
    for (const player of frame.players || []) {
      const px = pad + Number(player.x) / 100 * pitchW;
      const py = pad + Number(player.y) / 100 * pitchH;
      const distance = (x - px) ** 2 + (y - py) ** 2;
      if (distance < nearestDistance) {
        nearest = player;
        nearestDistance = distance;
      }
    }
    if (nearest) selectPlayer(nearest);
  }

  async function sendControl(action = null, favouritePlayer = undefined) {
    const payload = {
      speed: Number($("#speed").value),
      mode: $("#mode").value,
      language: $("#language").value,
      favourite_team: $("#favourite-team").value
    };
    if (favouritePlayer !== undefined) {
      state.favouritePlayer = favouritePlayer;
      payload.favourite_player = favouritePlayer;
    } else if (state.favouritePlayer) {
      payload.favourite_player = state.favouritePlayer;
    }
    if (action) payload.action = action;
    const snapshot = await api("/control", { method: "POST", body: JSON.stringify(payload) });
    renderSnapshot(snapshot);
    return snapshot;
  }

  function updateFrameFromSnapshot(snapshot) {
    if (!snapshot.frame) return;
    state.previousFrame = state.frame || snapshot.frame;
    state.frame = snapshot.frame;
    state.frameReceivedAt = performance.now();
  }

  function connectStream() {
    const stream = new EventSource(apiUrl("/stream"));
    stream.onopen = () => setConnection("CONNECTED");
    stream.onmessage = (event) => {
      try {
        const snapshot = JSON.parse(event.data);
        renderSnapshot(snapshot);
        updateFrameFromSnapshot(snapshot);
      } catch (error) {
        console.error("Could not parse match stream update", error);
      }
    };
    stream.addEventListener("overlay", (event) => {
      try { renderOverlay(JSON.parse(event.data)); }
      catch (error) { console.error("Could not parse overlay stream update", error); }
    });
    stream.onerror = () => setConnection("RECONNECTING");
  }

  $("#start-button").addEventListener("click", () => {
    void sendControl("start").catch((error) => showToast(error.message));
  });
  $("#stop-button").addEventListener("click", () => {
    void sendControl("stop").catch((error) => showToast(error.message));
  });
  for (const id of ["speed", "mode", "language", "favourite-team"]) {
    $(`#${id}`).addEventListener("change", () => {
      applyPreferences({
        speed: Number($("#speed").value), mode: $("#mode").value,
        language: $("#language").value, favourite_team: $("#favourite-team").value
      });
      void sendControl().catch((error) => showToast(error.message));
    });
  }
  $("#follow-player").addEventListener("click", () => {
    if (!state.selectedPlayer) return;
    const name = state.selectedPlayer.name;
    $("#mode").value = "Fan";
    state.mode = "Fan";
    void sendControl(null, name).then(() => renderPlayerCard()).catch((error) => showToast(error.message));
  });
  $("#player-card-close").addEventListener("click", () => {
    state.selectedPlayer = null;
    renderPlayerCard();
  });
  canvas.addEventListener("click", hitTestPlayer);
  canvas.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") hitTestPlayer({ clientX: canvas.getBoundingClientRect().left + canvas.clientWidth / 2, clientY: canvas.getBoundingClientRect().top + canvas.clientHeight / 2 });
  });
  for (const toggle of ["toggle-shape", "toggle-passes", "toggle-heat"]) {
    $(`#${toggle}`).addEventListener("change", drawPitch);
  }
  document.querySelectorAll("[data-panel]").forEach((button) => {
    button.addEventListener("click", () => activatePanel(button.dataset.panel));
  });
  $("#panel-close").addEventListener("click", closePanel);
  $("#panel-scrim").addEventListener("click", () => {
    closePanel();
    setHistory(false);
  });
  $("#history-button").addEventListener("click", () => setHistory(true));
  $("#latest-commentary").addEventListener("click", () => setHistory(true));
  $("#history-close").addEventListener("click", () => setHistory(false));
  $("#history-importance").addEventListener("change", renderHistory);
  $("#history-player").addEventListener("input", renderHistory);
  $("#insight-stack").addEventListener("click", (event) => {
    const card = event.target.closest("[data-insight-id]");
    if (!card) return;
    const id = card.dataset.insightId;
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "dismiss") {
      pinnedOverlays.delete(id);
      overlayExpiry.delete(id);
      state.overlays = state.overlays.filter((overlay) => overlay.overlay_id !== id);
    } else {
      if (pinnedOverlays.has(id)) pinnedOverlays.delete(id);
      else pinnedOverlays.add(id);
    }
    renderInsights();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closePanel();
      setHistory(false);
      return;
    }
    if (event.target.matches("input, select, textarea, button, [contenteditable=true]")) return;
    if (event.key.toLowerCase() === "h") {
      setHistory(!state.historyOpen);
      return;
    }
    if (/^[1-8]$/.test(event.key)) {
      const panelButton = document.querySelector(`[data-panel][aria-keyshortcuts="${event.key}"]`);
      panelButton?.click();
    }
  });
  window.addEventListener("resize", drawPitch);

  async function initialize() {
    try {
      const snapshot = await api("/state");
      renderSnapshot(snapshot);
      updateFrameFromSnapshot(snapshot);
    } catch (error) {
      showToast(error.message);
      $("#pitch-empty").querySelector("small").textContent = "API unavailable. Reconnecting to match service…";
    }
    try {
      const overlays = await api("/overlays/recent?limit=100");
      overlays.sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0)).forEach(renderOverlay);
    } catch (error) {
      console.error("Could not load recent overlay payloads", error);
    }
    void loadHistory();
    connectStream();
  }

  requestAnimationFrame(animatePitch);
  void initialize();
})();
