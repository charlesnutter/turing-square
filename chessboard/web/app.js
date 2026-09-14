/* The board screen.
 *
 * The Pi is the single source of truth: this sends commands and draws whatever
 * it is pushed. It reads a FEN to know where the pieces are and reads the legal
 * move list to know what may be dragged where -- but it never decides either.
 * There is deliberately no chess logic here, because that would be a second
 * rules engine disagreeing with the first one.
 */

const $ = (id) => document.getElementById(id);

const FILES = "abcdefgh";
const board = $("board");

let state = null;        // the last state we were pushed
let flipped = false;     // black at the bottom
let from = null;         // the square the player has picked up
let viewPly = null;      // a ply being reviewed, or null when live
let socket = null;
let backoff = 500;
let pendingPromo = null; // {from, to} waiting on a piece choice

/* ---------------- talking to the Pi ---------------- */

async function send(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "DELETE" : "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // The refusal carries a reason worth showing -- that is the whole point of
    // checking the move before it is queued.
    note(data.detail || `${res.status}`, true);
    return null;
  }
  return data;
}

function connect() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  socket = new WebSocket(url);

  socket.onopen = () => { backoff = 500; setConn(true); };
  socket.onmessage = (event) => { apply(JSON.parse(event.data)); };
  socket.onclose = () => {
    setConn(false);
    // A phone sleeps and the socket dies. Every state is a full snapshot, so
    // reconnecting is all the recovery there is to do.
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 10000);
  };
  socket.onerror = () => socket.close();
}

function setConn(up) {
  const dot = $("conn");
  dot.classList.toggle("on", up);
  dot.classList.toggle("off", !up);
  dot.setAttribute("aria-label", up ? "connected" : "reconnecting");
}

/* ---------------- drawing ---------------- */

function squares() {
  // a1..h8 in the order the grid wants them, honouring the flip
  const out = [];
  const ranks = flipped ? [0, 1, 2, 3, 4, 5, 6, 7] : [7, 6, 5, 4, 3, 2, 1, 0];
  const files = flipped ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];
  for (const r of ranks) for (const f of files) out.push(FILES[f] + (r + 1));
  return out;
}

function placement(fen) {
  /* FEN's first field into {square: pieceCode}. Reading where the pieces are is
     not deciding anything -- the rules stay on the Pi. */
  const map = {};
  const rows = fen.split(" ")[0].split("/");
  rows.forEach((row, i) => {
    let file = 0;
    for (const ch of row) {
      if (ch >= "1" && ch <= "8") { file += Number(ch); continue; }
      const square = FILES[file] + (8 - i);
      map[square] = (ch === ch.toUpperCase() ? "l" : "d") + ch.toLowerCase();
      file++;
    }
  });
  return map;
}

function shownFen() {
  if (!state || !state.running) return "8/8/8/8/8/8/8/8 w - - 0 1";
  if (viewPly === null) return state.fen;
  if (viewPly === 0) return state.start_fen;
  return state.history[viewPly - 1].fen;
}

function live() { return viewPly === null; }

function myMoves() {
  /* Legal moves as {from: [to...]}, only while looking at the live position. */
  const out = {};
  if (!state || !state.running || !live()) return out;
  for (const uci of state.legal_moves) {
    const a = uci.slice(0, 2), b = uci.slice(2, 4);
    (out[a] = out[a] || []).push(b);
  }
  return out;
}

function canTouch() {
  return Boolean(state && state.running && !state.over && !state.thinking && live());
}

function draw() {
  const fen = shownFen();
  const pieces = placement(fen);
  const moves = myMoves();
  const targets = from ? new Set(moves[from] || []) : null;
  const last = live() && state && state.last_move ? state.last_move : lastAt(viewPly);
  const checked = live() && state && state.check ? kingSquare(pieces, fen) : null;

  board.innerHTML = "";
  squares().forEach((name, i) => {
    const file = FILES.indexOf(name[0]), rank = Number(name[1]) - 1;
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "sq" + ((file + rank) % 2 === 0 ? " d" : "");
    cell.dataset.sq = name;
    cell.setAttribute("aria-label", name);

    if (last && (name === last.from || name === last.to)) cell.classList.add("last");
    if (checked === name) cell.classList.add("check");
    if (from === name) cell.classList.add("from");
    if (targets && targets.has(name)) {
      cell.classList.add("to");
      if (pieces[name]) cell.classList.add("occupied");
    }
    if (canTouch() && (moves[name] || targets)) cell.classList.add("playable");

    if (pieces[name]) {
      const img = document.createElement("img");
      img.src = `pieces/${pieces[name]}.svg`;
      img.alt = pieces[name];
      img.draggable = canTouch() && Boolean(moves[name]);
      cell.appendChild(img);
    }

    // coordinates on the outer edges only, as a printed board has them
    const col = i % 8, row = Math.floor(i / 8);
    if (row === 7) cell.appendChild(coord("f", name[0]));
    if (col === 0) cell.appendChild(coord("r", name[1]));

    board.appendChild(cell);
  });

  drawPanel();
}

function coord(kind, text) {
  const el = document.createElement("span");
  el.className = "coord " + kind;
  el.textContent = text;
  return el;
}

function lastAt(ply) {
  if (!state || !state.running || !ply) return null;
  const entry = state.history[ply - 1];
  return entry ? { from: entry.uci.slice(0, 2), to: entry.uci.slice(2, 4) } : null;
}

function kingSquare(pieces, fen) {
  const want = fen.split(" ")[1] === "w" ? "lk" : "dk";
  return Object.keys(pieces).find((sq) => pieces[sq] === want) || null;
}

/* ---------------- the panel ---------------- */

function drawPanel() {
  const running = Boolean(state && state.running);
  const history = running ? state.history : [];

  $("subtitle").textContent = running ? describe() : "no game";
  $("movesempty").hidden = history.length > 0;

  const list = $("movelist");
  list.innerHTML = "";
  for (let i = 0; i < history.length; i += 2) {
    const n = document.createElement("span");
    n.className = "n";
    n.textContent = `${i / 2 + 1}.`;
    list.appendChild(n);
    list.appendChild(moveButton(history[i], i + 1));
    if (history[i + 1]) list.appendChild(moveButton(history[i + 1], i + 2));
    else list.appendChild(document.createElement("span"));
  }

  const over = running && state.over;
  $("undo").disabled = !(running && state.can.takeback && live());
  $("resign").disabled = !running;
  $("gamehint").textContent = over ? state.outcome : "";
  $("gamehint").className = "hint";

  const veil = $("veil"), text = $("veiltext"), btn = $("veilbtn");
  btn.hidden = true;
  if (!running) {
    text.textContent = "No game running.";
    btn.hidden = false; btn.textContent = "Start one"; btn.onclick = () => showPane("game");
    veil.classList.add("show");
  } else if (!live()) {
    text.textContent = `Reviewing move ${viewPly} of ${state.ply}.`;
    btn.hidden = false; btn.textContent = "Back to the game";
    btn.onclick = () => { viewPly = null; draw(); };
    veil.classList.add("show");
  } else if (over) {
    text.textContent = state.outcome;
    veil.classList.add("show");
  } else if (state.thinking) {
    text.textContent = `${engineName()} is thinking…`;
    veil.classList.add("show");
  } else {
    veil.classList.remove("show");
  }
}

function moveButton(entry, ply) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = entry.san;
  if (viewPly === ply) b.setAttribute("aria-current", "true");
  b.onclick = () => {
    viewPly = viewPly === ply ? null : ply;
    from = null;
    draw();
  };
  return b;
}

function engineName() {
  if (!state || !state.running) return "The engine";
  return state.players[state.turn] || "The engine";
}

function describe() {
  const white = state.players.white, black = state.players.black;
  if (!white && !black) return "two players";
  return white ? `${white} (White)` : `${black} (Black)`;
}

/* ---------------- input ---------------- */

function pick(square) {
  if (!canTouch()) return;
  const moves = myMoves();

  if (from && (moves[from] || []).includes(square)) {
    submit(from, square);
    from = null;
    draw();
    return;
  }
  from = moves[square] ? square : null;
  draw();
}

function submit(a, b) {
  /* One path for every move, whoever made it. When there is a physical board
     this is where a move becomes a proposal rather than a fact. */
  const promotions = state.legal_moves.filter((m) => m.length === 5 && m.startsWith(a + b));
  if (promotions.length) { askPromotion(a, b); return; }
  send("/api/move", { move: a + b });
}

function askPromotion(a, b) {
  pendingPromo = { from: a, to: b };
  const light = state.turn === "white";
  const picks = $("picks");
  picks.innerHTML = "";
  for (const piece of ["q", "r", "b", "n"]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.title = { q: "Queen", r: "Rook", b: "Bishop", n: "Knight" }[piece];
    const img = document.createElement("img");
    img.src = `pieces/${light ? "l" : "d"}${piece}.svg`;
    img.alt = btn.title;
    btn.appendChild(img);
    btn.onclick = () => {
      $("sheet").classList.remove("show");
      send("/api/move", { move: pendingPromo.from + pendingPromo.to + piece });
      pendingPromo = null;
    };
    picks.appendChild(btn);
  }
  $("sheet").classList.add("show");
}

board.addEventListener("click", (e) => {
  const cell = e.target.closest(".sq");
  if (cell) pick(cell.dataset.sq);
});

board.addEventListener("dragstart", (e) => {
  const cell = e.target.closest(".sq");
  if (!cell || !canTouch()) { e.preventDefault(); return; }
  from = cell.dataset.sq;
  e.dataTransfer.setData("text/plain", from);
  draw();
});
board.addEventListener("dragover", (e) => { if (from) e.preventDefault(); });
board.addEventListener("drop", (e) => {
  e.preventDefault();
  const cell = e.target.closest(".sq");
  if (cell && from) pick(cell.dataset.sq);
});

/* ---------------- controls ---------------- */

$("flip").onclick = () => { flipped = !flipped; draw(); };

$("promocancel").onclick = () => {
  $("sheet").classList.remove("show");
  pendingPromo = null;
  from = null;
  draw();
};

$("undo").onclick = async () => { await send("/api/command", { command: "takeback" }); };
$("resign").onclick = async () => { await send("/api/game"); };

$("mode").onchange = () => {
  const ai = $("mode").value === "local-ai";
  $("airow").hidden = !ai;
  $("siderow").hidden = !ai;
};

$("side").onclick = () => {
  const black = $("side").dataset.black === "true";
  $("side").dataset.black = String(!black);
  $("side").textContent = black ? "White" : "Black";
};

$("skill").oninput = () => { $("skillval").textContent = $("skill").value; };

$("newgame").onclick = async () => {
  const mode = $("mode").value;
  const body = { mode };
  if (mode === "local-ai") {
    body.skill = Number($("skill").value);
    body.black = $("side").dataset.black === "true";
    body.movetime = 400;
  }
  const started = await send("/api/game", body);
  if (started) {
    viewPly = null;
    from = null;
    // Sit behind the board the way the physical one does: if an engine has
    // White, you are Black, so Black belongs at the bottom.
    flipped = Boolean(started.players && started.players.white);
    showPane("moves");
  }
};

function showPane(name) {
  document.querySelectorAll(".tabs button").forEach((b) => {
    const on = b.dataset.pane === name;
    b.setAttribute("aria-selected", String(on));
    $("pane-" + b.dataset.pane).hidden = !on;
  });
}
document.querySelector(".tabs").onclick = (e) => {
  const b = e.target.closest("button[data-pane]");
  if (b) showPane(b.dataset.pane);
};

function note(text, bad) {
  const hint = $("gamehint");
  hint.textContent = text;
  hint.className = "hint" + (bad ? " warn" : "");
  clearTimeout(note.timer);
  note.timer = setTimeout(() => {
    if (hint.textContent === text) { hint.textContent = ""; hint.className = "hint"; }
  }, 4000);
}

/* ---------------- state ---------------- */

function apply(next) {
  const started = !(state && state.running) && next.running;
  state = next;
  if (started || !next.running) { viewPly = null; from = null; }
  // A move landing while you were reviewing history must not yank you forward.
  if (viewPly !== null && viewPly > next.ply) viewPly = null;
  draw();
}

async function boot() {
  const res = await fetch("/api/state");
  apply(await res.json());
  connect();
}

boot();
