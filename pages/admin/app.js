/* 分身的世界 · 后台管理页逻辑(bridge SDK → 本插件 Web API) */
/* global AstrBotPluginPage */
(function () {
  "use strict";

  const P = window.AstrBotPluginPage;
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  function toast(msg) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2600);
  }

  // sandbox iframe 里原生 confirm/alert 被禁 → 自绘确认弹窗
  function openModal(title, bodyHtml, buttons) {
    $("#modal-title").textContent = title || "";
    $("#modal-body").innerHTML = bodyHtml || "";
    const foot = $("#modal-foot");
    foot.innerHTML = "";
    (buttons || []).forEach((b) => {
      const btn = document.createElement("button");
      btn.className = "btn " + (b.style || "");
      btn.textContent = b.label;
      btn.onclick = async () => {
        try {
          if ((await b.onClick?.()) === false) return;
        } catch (e) {
          toast("❌ " + e.message);
          return;
        }
        closeModal();
      };
      foot.appendChild(btn);
    });
    $("#modal-mask").classList.add("show");
  }
  function closeModal() { $("#modal-mask").classList.remove("show"); }
  function confirmAction(title, text, onOk) {
    openModal(title, `<p style="margin:4px 0">${esc(text)}</p>`, [
      { label: "取消" },
      { label: "确认执行", style: "danger", onClick: onOk },
    ]);
  }

  // ── API 封装(仅 bridge:鉴权由 Dashboard 统一处理) ──
  async function apiGet(ep, params) {
    if (!P?.apiGet) throw new Error("bridge SDK 未加载:请从 Dashboard 插件页面打开");
    return await P.apiGet(ep, params);
  }
  async function apiPost(ep, body) {
    if (!P?.apiPost) throw new Error("bridge SDK 未加载:请从 Dashboard 插件页面打开");
    return await P.apiPost(ep, body);
  }

  let GID = "";
  let CHARS = [];
  let NPCS = [];
  let LOG = { uid: "", offset: 0 };
  let MEM = { uid: "" };

  const fmtTs = (v) => {
    const t = typeof v === "number" ? new Date(v * 1000) : new Date(v);
    return isNaN(t) ? "-" : `${t.getMonth() + 1}/${t.getDate()} ${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`;
  };

  // ── Tab 切换 ──
  let CURRENT = "overview";
  const TAB_LOADERS = {
    overview: loadOverview, chars: loadChars, world: loadWorld,
    events: loadEvents, logs: loadLogs, mems: loadMems,
    config: loadConfig, ops: () => {},
  };
  function switchTab(name) {
    CURRENT = name;
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    (TAB_LOADERS[name] || (() => {}))().catch((e) => toast("❌ " + e.message));
  }

  async function loadCharsThenFill(selectSel) {
    const d = await apiGet("/admin/api/chars", { gid: GID });
    CHARS = d.chars || [];
    const sel = $(selectSel);
    const keep = sel.value;
    sel.innerHTML = `<option value="">全部</option>` +
      CHARS.map((c) => `<option value="${esc(c.uid)}"${c.uid === keep ? " selected" : ""}>${esc(c.name)}</option>`).join("");
    return CHARS;
  }

  // ── 总览 ──
  async function loadOverview() {
    const d = await apiGet("/admin/api/overview");
    const sel = $("#gid");
    const keep = sel.value || GID;
    sel.innerHTML = d.groups.map((g) =>
      `<option value="${esc(g.gid)}"${g.gid === keep ? " selected" : ""}>${esc(g.gid)} · ${esc(g.world ? g.world.name : "未初始化")}</option>`).join("");
    if (!GID) GID = sel.value || "";
    sel.value = GID;
    const g = d.groups.find((x) => x.gid === GID) || d.groups[0];
    if (!g) { $("#main").innerHTML = '<p class="muted">还没有任何群初始化过世界。</p>'; return; }
    GID = g.gid;
    $("#ov-cards").innerHTML = [
      ["世界", g.world ? g.world.name : "未初始化", g.world ? g.world.genre : ""],
      ["成员", g.chars.length, g.chars.map((c) => c.name).join("、").slice(0, 40)],
      ["待抉择事件", g.pending_events, "45 分钟未抉择会平淡收场"],
    ].map(([lbl, num, sub]) => `
      <div class="card"><div class="num">${esc(num)}</div>
      <div class="lbl">${esc(lbl)}</div>${sub ? `<div class="lbl muted">${esc(sub)}</div>` : ""}</div>`).join("");
    $("#ov-cfg").textContent = JSON.stringify(g.config, null, 2);
  }

  // ── 角色 ──
  async function loadChars() {
    const d = await apiGet("/admin/api/chars", { gid: GID });
    CHARS = d.chars || [];
    $("#chars-tbl tbody").innerHTML = CHARS.map((c) => `
      <tr><td>${esc(c.name)}</td><td><code>${esc(c.uid)}</code></td><td>${esc(c.gender)}</td>
      <td>${c.level}</td><td>${c.gold}</td><td>${c.stamina}/${c.mood}</td><td>${esc(c.title)}</td>
      <td><button class="btn tiny" data-uid="${esc(c.uid)}">编辑</button></td></tr>`).join("");
    $$("#chars-tbl tbody button").forEach((b) => {
      b.onclick = () => editChar(b.dataset.uid).catch((e) => toast("❌ " + e.message));
    });
    $("#cedit").innerHTML = "";
  }

  async function editChar(uid) {
    const d = await apiGet(`/admin/api/char?gid=${encodeURIComponent(GID)}&uid=${encodeURIComponent(uid)}`);
    const c = d.char;
    const A = c.attrs || {};
    $("#cedit").innerHTML = `
      <div class="card"><h3>编辑 · ${esc(c.name)}</h3>
      <div class="grid">
        <label>名字 <input class="w" id="ce-name" value="${esc(c.name)}"></label>
        <label>性别 <input class="w" id="ce-gender" value="${esc(c.gender)}"></label>
        <label>称号 <input class="w" id="ce-title" value="${esc(c.title)}"></label>
        <label>性格标签(逗号分隔)<input class="w" id="ce-tags" value="${esc((c.tags || []).join(", "))}"></label>
      </div>
      <label>背景设定</label><textarea class="w" id="ce-back">${esc(c.backstory)}</textarea>
      <div class="grid">
        <label>等级 <input type="number" class="w" id="ce-level" value="${c.level}"></label>
        <label>经验 <input type="number" class="w" id="ce-exp" value="${c.exp}"></label>
        <label>金币 <input type="number" class="w" id="ce-gold" value="${c.gold}"></label>
        <label>体力 <input type="number" class="w" id="ce-stamina" value="${c.stamina}"></label>
        <label>心情 <input type="number" class="w" id="ce-mood" value="${c.mood}"></label>
      </div>
      <label>六维(力量/敏捷/智力/魅力/幸运/精神)</label>
      <div class="toolbar">${["force", "agility", "intellect", "charm", "luck", "sanity"].map((k) =>
        `<input type="number" style="width:90px" id="ce-${k}" value="${A[k] ?? 0}" title="${k}">`).join("")}</div>
      <label>flags(JSON)</label><textarea class="w" id="ce-flags">${esc(JSON.stringify(c.flags || {}, null, 1))}</textarea>
      <div class="row-end">
        <button class="btn primary" id="ce-save">保存</button>
        <button class="btn danger" id="ce-del">删除角色</button>
        <span class="muted">删除会连同日志/记忆/羁绊一并清空</span>
      </div>
      <h3 style="margin-top:16px">羁绊</h3>
      <table class="tbl">${d.rels.map((r) => `
        <tr><td>${esc(r.name)}</td><td><code>${esc(r.uid)}</code></td>
        <td><input type="number" style="width:80px" id="rel-${esc(r.uid)}" value="${r.score}"></td>
        <td>${esc(r.state || "-")}</td>
        <td><button class="btn tiny" data-b="${esc(r.uid)}">保存</button></td></tr>`).join("") || "<tr><td class='muted'>暂无</td></tr>"}</table>
      <h3 style="margin-top:16px">最近日志</h3>
      <pre class="code">${esc(d.logs.map((l) => `[${fmtTs(l.ts)}] ${l.text}`).join("\n") || "无")}</pre></div>`;
    $("#ce-save").onclick = async () => {
      let flags = {};
      try { flags = JSON.parse($("#ce-flags").value || "{}"); } catch { return toast("❌ flags 不是合法 JSON"); }
      const vn = (id) => parseInt($(id).value || "0", 10) || 0;
      await apiPost("/admin/api/char", {
        gid: GID, uid, name: $("#ce-name").value, gender: $("#ce-gender").value,
        title: $("#ce-title").value, backstory: $("#ce-back").value,
        tags: $("#ce-tags").value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean),
        level: vn("#ce-level"), exp: vn("#ce-exp"), gold: vn("#ce-gold"),
        stamina: vn("#ce-stamina"), mood: vn("#ce-mood"),
        attrs: { force: vn("#ce-force"), agility: vn("#ce-agility"), intellect: vn("#ce-intellect"),
                 charm: vn("#ce-charm"), luck: vn("#ce-luck"), sanity: vn("#ce-sanity") },
        flags,
      });
      toast("✅ 已保存");
      loadChars();
    };
    $("#ce-del").onclick = () => confirmAction("删除角色",
      `将删除「${c.name}」,日志/记忆/羁绊一并清空,不可恢复。`, async () => {
        await apiPost("/admin/api/char/delete", { gid: GID, uid });
        toast("✅ 已删除");
        loadChars();
      });
    $$("#cedit [data-b]").forEach((b) => {
      b.onclick = async () => {
        // 生活角色 uid 形如 npc:<群>:<名字>(含冒号),不可用 querySelector,#
        // 否则冒号会被当成伪类解析 —— 必须用 getElementById
        const input = document.getElementById("rel-" + b.dataset.b);
        const score = parseInt((input && input.value) || "0", 10) || 0;
        await apiPost("/admin/api/rel", { gid: GID, a: uid, b: b.dataset.b, score });
        toast("✅ 羁绊已保存");
      };
    });
  }

  // ── 世界与NPC ──
  async function loadWorld() {
    const d = await apiGet("/admin/api/world", { gid: GID });
    const cur = d.worlds.find((w) => w.visited) || d.worlds[0];
    if (!cur) { $("#world-wrap").innerHTML = '<p class="muted">该群还没有世界。</p>'; return; }
    NPCS = cur.npcs || [];
    const npcRow = (n, i) => `
      <tr>
        <td><input value="${esc(n.name)}" data-i="${i}" data-k="name" style="width:90px"></td>
        <td><input value="${esc(n.role)}" data-i="${i}" data-k="role" style="width:110px"></td>
        <td><input value="${esc(n.persona)}" data-i="${i}" data-k="persona" style="width:180px"></td>
        <td><input value="${esc(n.hook)}" data-i="${i}" data-k="hook" style="width:140px"></td>
        <td><input value="${esc(n.daily)}" data-i="${i}" data-k="daily" style="width:120px"></td>
        <td><input value="${esc(n.quirk)}" data-i="${i}" data-k="quirk" style="width:110px"></td>
        <td>${n.builtin ? '<span class="pill">系统</span>' : ""}<button class="btn tiny danger" data-del="${i}">删</button></td>
      </tr>`;
    $("#world-wrap").innerHTML = `
      <div class="card"><h3>当前世界 · 《${esc(cur.name)}》${cur.visited ? "" : "(未降临)"}</h3>
      <div class="grid">
        <label>名字 <input class="w" id="w-name" value="${esc(cur.name)}"></label>
        <label>题材 <input class="w" id="w-genre" value="${esc(cur.genre)}"></label>
        <label>氛围 <input class="w" id="w-atmosphere" value="${esc(cur.atmosphere)}"></label>
      </div>
      <label>描述</label><textarea class="w" id="w-desc">${esc(cur.desc)}</textarea>
      <div class="grid">
        <label>规则(每行一条)<textarea class="w" id="w-rules">${esc((cur.rules || []).join("\n"))}</textarea></label>
        <label>独特之处(每行一条)<textarea class="w" id="w-features">${esc((cur.features || []).join("\n"))}</textarea></label>
      </div>
      <div class="row-end"><button class="btn primary" id="w-save">保存世界</button></div></div>
      <div class="card"><h3>NPC(保存为整表替换)</h3>
      <div class="table-wrap"><table class="tbl" id="npc-tbl">
        <thead><tr><th>名字</th><th>身份</th><th>人设</th><th>钩子</th><th>日常</th><th>怪癖</th><th></th></tr></thead>
        <tbody>${NPCS.map(npcRow).join("")}</tbody>
      </table></div>
      <div class="row-end">
        <button class="btn" id="npc-add">+ 添加 NPC</button>
        <button class="btn primary" id="npc-save">保存全部 NPC</button>
      </div></div>
      <div class="card"><h3>全部世界</h3>${d.worlds.map((w) =>
        `<span class="pill">${w.visited ? "✅" : "🔒"} ${esc(w.name)} [${esc(w.genre)}]</span>`).join("")}</div>`;
    $$("#npc-tbl input").forEach((inp) => {
      inp.onchange = () => { NPCS[parseInt(inp.dataset.i, 10)][inp.dataset.k] = inp.value; };
    });
    $$("#npc-tbl [data-del]").forEach((b) => {
      b.onclick = () => { NPCS.splice(parseInt(b.dataset.del, 10), 1); loadWorld(); };
    });
    $("#npc-add").onclick = () => {
      NPCS.push({ name: "新NPC", role: "居民", persona: "", hook: "", daily: "", quirk: "", builtin: 0 });
      loadWorld();
    };
    $("#npc-save").onclick = async () => {
      await apiPost("/admin/api/world", { gid: GID, npcs: NPCS });
      toast("✅ NPC 已保存");
    };
    $("#w-save").onclick = async () => {
      const ls = (id) => $(id).value.split(/\n/).map((s) => s.trim()).filter(Boolean);
      await apiPost("/admin/api/world", {
        gid: GID, name: $("#w-name").value, genre: $("#w-genre").value,
        atmosphere: $("#w-atmosphere").value, desc: $("#w-desc").value,
        rules: ls("#w-rules"), features: ls("#w-features"),
      });
      toast("✅ 世界已保存");
    };
  }

  // ── 事件 ──
  async function loadEvents() {
    const d = await apiGet("/admin/api/events", { gid: GID });
    const pend = d.events.filter((e) => e.state === "pending");
    $("#events-wrap").innerHTML = `
      <div class="card"><h3>待抉择(${pend.length})</h3>
      ${pend.map((e) => `
        <div style="border-bottom:1px solid var(--line);padding:8px 0">
          <b>#${e.id} ${esc(e.title)}</b> <span class="pill">${esc(e.uid || "全员")}</span>
          <button class="btn tiny danger" style="float:right" data-exp="${e.id}">平淡收场</button>
          <div class="muted">${esc(e.scene)}</div>
        </div>`).join("") || '<p class="muted">无</p>'}</div>
      <div class="card"><h3>最近事件</h3><div class="table-wrap"><table class="tbl">
        <thead><tr><th>#</th><th>标题</th><th>归属</th><th>状态</th><th>已选</th></tr></thead>
        <tbody>${d.events.map((e) => `
          <tr><td>${e.id}</td><td>${esc(e.title)}</td><td class="muted">${esc(e.uid || "全员")}</td>
          <td>${e.state === "pending" ? '<span class="ok">待抉择</span>' : esc(e.state)}</td>
          <td>${e.chosen >= 0 ? esc((e.options[e.chosen] || {}).label || "") : "-"}</td></tr>`).join("")}</tbody>
      </table></div></div>`;
    $$("#events-wrap [data-exp]").forEach((b) => {
      b.onclick = () => confirmAction("强制收场", `将把事件 #${b.dataset.exp} 平淡收场(不再等待抉择)。`, async () => {
        await apiPost("/admin/api/event/expire", { gid: GID, id: parseInt(b.dataset.exp, 10) });
        toast("✅ 已收场");
        loadEvents();
      });
    });
  }

  // ── 日志 ──
  async function loadLogs() {
    const chars = await loadCharsThenFill("#log-uid");
    if (!$("#log-uid").value && chars.length) $("#log-uid").value = "";
    const q = { gid: GID, offset: LOG.offset, limit: 50 };
    if (LOG.uid) q.uid = LOG.uid;
    const d = await apiGet("/admin/api/logs", q);
    $("#log-total").textContent = `共 ${d.total} 条`;
    $("#logs-tbl tbody").innerHTML = d.logs.map((l) => `
      <tr><td class="muted">${fmtTs(l.ts)}</td><td class="muted">${esc(l.uid || "-")}</td>
      <td>${esc(l.kind)}</td><td>${esc(l.text)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">无</td></tr>';
  }

  // ── 记忆 ──
  async function loadMems() {
    const chars = await loadCharsThenFill("#mem-uid");
    if (!$("#mem-uid").value) $("#mem-uid").value = MEM.uid;
    const q = { gid: GID };
    if (MEM.uid) q.uid = MEM.uid;
    const d = await apiGet("/admin/api/memories", q);
    $("#mem-total").textContent = `共 ${d.memories.length} 条`;
    $("#mems-tbl tbody").innerHTML = d.memories.map((m) => `
      <tr><td><input type="checkbox" class="mk" value="${m.id}"></td><td>${m.id}</td>
      <td class="muted">${esc(m.uid || "-")}</td><td>${esc(m.scope)}</td><td>${esc(m.text)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">无</td></tr>';
  }

  // ── 配置 ──
  async function loadConfig() {
    const d = await apiGet("/admin/api/config", { gid: GID });
    for (const k of Object.keys(d)) {
      const el = $("#cf-" + k);
      if (el) el.value = d[k];
    }
  }

  // ── 操作 ──
  async function doTrigger(kind) {
    $("#op-out").textContent = "执行中…";
    try {
      const d = await apiPost("/admin/api/trigger", { gid: GID, kind });
      $("#op-out").textContent = d.message;
      toast("✅ 已完成");
    } catch (e) {
      $("#op-out").textContent = "失败:" + e.message;
      toast("❌ " + e.message);
    }
  }

  // ── 启动 ──
  async function boot() {
    if (!P?.apiGet) {
      $("#nosbridge").style.display = "block";
      $("#main").innerHTML = '<p class="muted">请从 Dashboard「插件 → 分身的世界 → 页面」打开本页(bridge 未加载,无法鉴权调用)。</p>';
      return;
    }
    try { await P.ready?.(); } catch { /* 忽略 handshake 异常 */ }
    const ctx = P.getContext?.();
    if (ctx?.username) $("#who").textContent = "@" + ctx.username;
    try {
      await loadOverview(); // 拉取群列表并填充 gid 选择器,设定默认 GID
      $$(".tab").forEach((t) => { t.onclick = () => switchTab(t.dataset.tab); });
      $("#gid").onchange = () => { GID = $("#gid").value; switchTab(CURRENT); };
      $("#btn-refresh").onclick = () => switchTab(CURRENT);
      $("#log-uid").onchange = () => { LOG.uid = $("#log-uid").value; LOG.offset = 0; loadLogs(); };
      $("#log-prev").onclick = () => { LOG.offset = Math.max(0, LOG.offset - 50); loadLogs(); };
      $("#log-next").onclick = () => { LOG.offset += 50; loadLogs(); };
      $("#mem-uid").onchange = () => { MEM.uid = $("#mem-uid").value; loadMems(); };
      $("#mem-del").onclick = () => {
        const ids = $$(".mk:checked").map((x) => parseInt(x.value, 10));
        if (!ids.length) return toast("未勾选任何记忆");
        confirmAction("删除记忆", `将删除 ${ids.length} 条记忆,不可恢复。`, async () => {
          const r = await apiPost("/admin/api/memory/delete", { gid: GID, ids });
          toast(`✅ 已删除 ${r.deleted} 条`);
          loadMems();
        });
      };
      $("#cf-save").onclick = async () => {
        const body = { gid: GID };
        for (const k of ["event_min", "event_max", "shift_percent", "user_world_share", "travel_cooldown_h"]) {
          body[k] = parseInt($("#cf-" + k).value || "0", 10) || 0;
        }
        await apiPost("/admin/api/config", body);
        toast("✅ 已保存");
      };
      $("#op-event").onclick = () => doTrigger("event");
      $("#op-shift").onclick = () => doTrigger("shift");
      $("#op-morning").onclick = () => doTrigger("morning");
      switchTab("overview");
    } catch (e) {
      $("#main").innerHTML = `<p class="bad">连接失败:${esc(e.message)}</p>
        <p class="muted">请确认已登录 Dashboard,并从「插件 → 分身的世界 → 页面」打开本页。</p>`;
    }
  }

  boot();
})();
