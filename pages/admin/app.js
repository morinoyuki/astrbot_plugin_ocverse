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
    events: loadEvents, logs: loadLogs, mems: loadMems, kb: loadKb,
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
      <td>${c.level}</td><td>${c.gold}</td><td>${c.stamina}/${c.mood}</td><td>${c.hp ?? 100}</td><td>${esc(c.title)}</td>
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
        <label>生命 <input type="number" class="w" id="ce-hp" value="${c.hp ?? 100}"></label>
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
        stamina: vn("#ce-stamina"), mood: vn("#ce-mood"), hp: vn("#ce-hp"),
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

  // ── 世界与NPC(可选任意世界;设施/NPC 整表编辑) ──
  let WORLDS = [];
  let WID = null;
  let NPCS = [];
  let INFRA = [];
  let ZONES = [];
  let HEALS = [];
  async function loadWorld() {
    const d = await apiGet("/admin/api/world", { gid: GID });
    WORLDS = d.worlds || [];
    if (!WORLDS.length) { $("#world-wrap").innerHTML = '<p class="muted">该群还没有世界。</p>'; return; }
    if (!WORLDS.find((w) => w.id === WID)) WID = (WORLDS.find((w) => w.visited) || WORLDS[0]).id;
    renderWorldEditor();
  }

  function renderWorldEditor() {
    const cur = WORLDS.find((w) => w.id === WID);
    if (!cur) return;
    NPCS = cur.npcs || [];
    INFRA = cur.infra || [];
    ZONES = cur.zones || [];
    HEALS = cur.heal_items || [];
    const opts = WORLDS.map((w) =>
      `<option value="${w.id}"${w.id === WID ? " selected" : ""}>${w.visited ? "✅" : "🔒"} ${esc(w.name)} [${esc(w.genre)}]</option>`).join("");
    const npcRow = (n, i) => `
      <tr>
        <td><input value="${esc(n.name)}" data-i="${i}" data-k="name" style="width:90px"></td>
        <td><input value="${esc(n.role)}" data-i="${i}" data-k="role" style="width:110px"></td>
        <td><input value="${esc(n.persona)}" data-i="${i}" data-k="persona" style="width:180px"></td>
        <td><input value="${esc(n.hook)}" data-i="${i}" data-k="hook" style="width:140px"></td>
        <td><input value="${esc(n.daily)}" data-i="${i}" data-k="daily" style="width:120px"></td>
        <td><input value="${esc(n.quirk)}" data-i="${i}" data-k="quirk" style="width:110px"></td>
        <td>${n.builtin ? '<span class="pill">系统</span>' : ""}<button class="btn tiny danger" data-n="${i}">删</button></td>
      </tr>`;
    const infraRow = (f, i) => `
      <tr>
        <td><input value="${esc(f.kind)}" data-fi="${i}" data-k="kind" style="width:90px" placeholder="类型"></td>
        <td><input value="${esc(f.name)}" data-fi="${i}" data-k="name" style="width:110px" placeholder="名字"></td>
        <td><input value="${esc(f.desc)}" data-fi="${i}" data-k="desc" style="width:220px" placeholder="功能/氛围"></td>
        <td><input value="${esc(f.work)}" data-fi="${i}" data-k="work" style="width:130px" placeholder="打工职业(可空)"></td>
        <td><button class="btn tiny danger" data-f="${i}">删</button></td>
      </tr>`;
    const zoneRow = (z, i) => `
      <tr>
        <td><input value="${esc(z.kind)}" data-zi="${i}" data-zk="kind" style="width:80px" placeholder="类型"></td>
        <td><input value="${esc(z.name)}" data-zi="${i}" data-zk="name" style="width:100px" placeholder="名字"></td>
        <td><input value="${esc(z.desc)}" data-zi="${i}" data-zk="desc" style="width:180px" placeholder="描述"></td>
        <td><input type="number" min="1" max="5" value="${z.danger ?? 1}" data-zi="${i}" data-zk="danger" style="width:56px"></td>
        <td><input value="${esc((z.enemies || []).map((e) => e.name).join(", "))}" data-zi="${i}" data-zk="enemies" style="width:150px" placeholder="敌人1, 敌人2"></td>
        <td><input value="${esc((z.loot || []).join(", "))}" data-zi="${i}" data-zk="loot" style="width:130px" placeholder="素材1, 素材2"></td>
        <td><button class="btn tiny danger" data-z="${i}">删</button></td>
      </tr>`;
    const healRow = (h, i) => `
      <tr>
        <td><input value="${esc(h.name)}" data-hi="${i}" data-hk="name" style="width:110px" placeholder="名字"></td>
        <td><input value="${esc(h.note)}" data-hi="${i}" data-hk="note" style="width:220px" placeholder="功效一句话"></td>
        <td><input type="number" value="${h.price ?? 30}" data-hi="${i}" data-hk="price" style="width:80px"></td>
        <td><input type="number" value="${h.heal ?? 30}" data-hi="${i}" data-hk="heal" style="width:80px"></td>
        <td><button class="btn tiny danger" data-h="${i}">删</button></td>
      </tr>`;
    $("#world-wrap").innerHTML = `
      <div class="toolbar">编辑目标:<select id="w-sel" class="sel">${opts}</select>
        <span class="muted">${cur.visited ? "当前世界" : "沉眠世界(未降临)"} · #${cur.id}</span></div>
      <div class="card"><h3>世界档案</h3>
      <div class="grid">
        <label>名字 <input class="w" id="w-name" value="${esc(cur.name)}"></label>
        <label>题材 <input class="w" id="w-genre" value="${esc(cur.genre)}"></label>
        <label>氛围 <input class="w" id="w-atmosphere" value="${esc(cur.atmosphere)}"></label>
      </div>
      <label>描述</label><textarea class="w" id="w-desc" rows="5">${esc(cur.desc)}</textarea>
      <label>规则(每行一条,最多 4 条)</label><textarea class="w ta-lg" id="w-rules">${esc((cur.rules || []).join("\n"))}</textarea>
      <label>独特之处(每行一条,最多 5 条)</label><textarea class="w ta-lg" id="w-features">${esc((cur.features || []).join("\n"))}</textarea>
      <div class="row-end">
        <button class="btn primary" id="w-save">保存世界档案</button>
        <button class="btn" id="content-regen">🎲 AI 重绘区域/治疗物品</button>
      </div></div>
      <div class="card"><h3>基础设施(保存为整表替换;work 非空的设施可兼职)</h3>
      <div class="table-wrap"><table class="tbl" id="infra-tbl">
        <thead><tr><th>类型</th><th>名字</th><th>功能/氛围</th><th>打工职业</th><th></th></tr></thead>
        <tbody>${INFRA.map(infraRow).join("")}</tbody>
      </table></div>
      <div class="row-end">
        <button class="btn" id="infra-add">+ 添加设施</button>
        <button class="btn" id="infra-regen">🎲 AI 重新生成设施</button>
        <button class="btn primary" id="infra-save">保存全部设施</button>
      </div></div>
      <div class="card"><h3>NPC(保存为整表替换)</h3>
      <div class="table-wrap"><table class="tbl" id="npc-tbl">
        <thead><tr><th>名字</th><th>身份</th><th>人设</th><th>钩子</th><th>日常</th><th>怪癖</th><th></th></tr></thead>
        <tbody>${NPCS.map(npcRow).join("")}</tbody>
      </table></div>
      <div class="row-end">
        <button class="btn" id="npc-add">+ 添加 NPC</button>
        <button class="btn primary" id="npc-save">保存全部 NPC</button>
      </div></div>
      <div class="card"><h3>危险区域(每日自动变动;与讨伐任务/素材联动;敌人/素材用逗号分隔)</h3>
      <div class="table-wrap"><table class="tbl" id="zone-tbl">
        <thead><tr><th>类型</th><th>名字</th><th>描述</th><th>危险度1-5</th><th>敌人(逗号分隔)</th><th>素材(逗号分隔)</th><th></th></tr></thead>
        <tbody>${ZONES.map(zoneRow).join("")}</tbody>
      </table></div>
      <div class="row-end">
        <button class="btn" id="zone-add">+ 添加区域</button>
        <button class="btn primary" id="zone-save">保存全部区域</button>
      </div></div>
      <div class="card"><h3>治疗物品(店铺售卖/掉落/「治疗」使用;低到高三档)</h3>
      <div class="table-wrap"><table class="tbl" id="heal-tbl">
        <thead><tr><th>名字</th><th>功效</th><th>售价</th><th>恢复量</th><th></th></tr></thead>
        <tbody>${HEALS.map(healRow).join("")}</tbody>
      </table></div>
      <div class="row-end">
        <button class="btn" id="heal-add">+ 添加治疗物品</button>
        <button class="btn primary" id="heal-save">保存全部治疗物品</button>
      </div></div>`;
    $("#w-sel").onchange = () => { WID = parseInt($("#w-sel").value, 10); renderWorldEditor(); };
    const ls = (id) => $(id).value.split(/\n/).map((s) => s.trim()).filter(Boolean);
    $("#w-save").onclick = async () => {
      await apiPost("/admin/api/world", {
        gid: GID, world_id: WID, name: $("#w-name").value, genre: $("#w-genre").value,
        atmosphere: $("#w-atmosphere").value, desc: $("#w-desc").value,
        rules: ls("#w-rules"), features: ls("#w-features"),
      });
      toast("✅ 世界已保存");
    };
    $$("#infra-tbl input").forEach((inp) => {
      inp.onchange = () => { INFRA[parseInt(inp.dataset.fi, 10)][inp.dataset.k] = inp.value; };
    });
    $$("#infra-tbl [data-f]").forEach((b) => {
      b.onclick = () => { INFRA.splice(parseInt(b.dataset.f, 10), 1); renderWorldEditor(); };
    });
    $("#infra-add").onclick = () => {
      INFRA.push({ kind: "商店", name: "新设施", desc: "", work: "" });
      renderWorldEditor();
    };
    $("#infra-save").onclick = async () => {
      await apiPost("/admin/api/world", { gid: GID, world_id: WID, infra: INFRA });
      toast("✅ 设施已保存");
    };
    $("#infra-regen").onclick = () => confirmAction("AI 重新生成设施",
      "将丢弃当前设施清单,由 AI 贴合世界观重新规划(20~28 个,覆盖生存必要设施、社交娱乐场所与打工位)。继续?",
      async () => {
        toast("⏳ AI 规划中,可能需要一点时间…");
        const r = await apiPost("/admin/api/infra/regen", { gid: GID, world_id: WID });
        const w = WORLDS.find((x) => x.id === WID);
        if (w) w.infra = r.infra || [];
        toast("✅ " + String(r.message || "已重新生成").split("\n")[0]);
        renderWorldEditor();
    });
    $$("#npc-tbl input").forEach((inp) => {
      inp.onchange = () => { NPCS[parseInt(inp.dataset.i, 10)][inp.dataset.k] = inp.value; };
    });
    $$("#npc-tbl [data-n]").forEach((b) => {
      b.onclick = () => { NPCS.splice(parseInt(b.dataset.n, 10), 1); renderWorldEditor(); };
    });
    $("#npc-add").onclick = () => {
      NPCS.push({ name: "新NPC", role: "居民", persona: "", hook: "", daily: "", quirk: "", builtin: 0 });
      renderWorldEditor();
    };
    $("#npc-save").onclick = async () => {
      await apiPost("/admin/api/world", { gid: GID, world_id: WID, npcs: NPCS });
      toast("✅ NPC 已保存");
    };
    $$("#zone-tbl input").forEach((inp) => {
      inp.onchange = () => {
        const z = ZONES[parseInt(inp.dataset.zi, 10)];
        const k = inp.dataset.zk;
        if (k === "enemies") {
          z.enemies = inp.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean).map((nm) => ({ name: nm.slice(0, 10), desc: "" }));
        } else if (k === "loot") {
          z.loot = inp.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean).slice(0, 3);
        } else if (k === "danger") {
          z.danger = Math.max(1, Math.min(5, parseInt(inp.value, 10) || 1));
        } else {
          z[k] = inp.value;
        }
      };
    });
    $$("#zone-tbl [data-z]").forEach((b) => {
      b.onclick = () => { ZONES.splice(parseInt(b.dataset.z, 10), 1); renderWorldEditor(); };
    });
    $("#zone-add").onclick = () => {
      ZONES.push({ kind: "野外", name: "新区域", desc: "", danger: 1, enemies: [], loot: [] });
      renderWorldEditor();
    };
    $("#zone-save").onclick = async () => {
      await apiPost("/admin/api/world", { gid: GID, world_id: WID, zones: ZONES });
      toast("✅ 危险区域已保存");
    };
    $$("#heal-tbl input").forEach((inp) => {
      inp.onchange = () => {
        const h = HEALS[parseInt(inp.dataset.hi, 10)];
        const k = inp.dataset.hk;
        if (k === "price" || k === "heal") {
          h[k] = Math.max(10, parseInt(inp.value, 10) || 30);
        } else {
          h[k] = inp.value;
        }
      };
    });
    $$("#heal-tbl [data-h]").forEach((b) => {
      b.onclick = () => { HEALS.splice(parseInt(b.dataset.h, 10), 1); renderWorldEditor(); };
    });
    $("#heal-add").onclick = () => {
      HEALS.push({ name: "新药", note: "", price: 30, heal: 30 });
      renderWorldEditor();
    };
    $("#heal-save").onclick = async () => {
      await apiPost("/admin/api/world", { gid: GID, world_id: WID, heal_items: HEALS });
      toast("✅ 治疗物品已保存");
    };
    $("#content-regen").onclick = () => confirmAction("AI 重绘区域/治疗物品",
      "将丢弃当前危险区域与治疗物品名录,由 AI 贴合世界观重新生成(3~6 片区域 + 3 档治疗物品)。继续?",
      async () => {
        toast("⏳ AI 规划中,可能需要一点时间…");
        const r = await apiPost("/admin/api/content/regen", { gid: GID, world_id: WID });
        const w = WORLDS.find((x) => x.id === WID);
        if (w) { w.zones = r.zones || []; w.heal_items = r.heal_items || []; }
        toast("✅ " + String(r.message || "已重绘").split("\n")[0]);
        renderWorldEditor();
      });
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

  // ── 知识库 ──
  let KB = { source: "", q: "", entries: [] };
  async function loadKb() {
    const d = await apiGet("/admin/api/kb", { gid: GID });
    KB.entries = d.entries || [];
    $("#kb-total").textContent = `共 ${d.total} 条` + (d.sources?.length ? ` · 来源 ${d.sources.length} 个` : "");
    const sel = $("#kb-source");
    const keep = sel.value;
    sel.innerHTML = `<option value="">全部</option>` +
      (d.sources || []).map((s) => `<option value="${esc(s)}"${s === keep ? " selected" : ""}>${esc(s)}</option>`).join("");
    renderKbRows();
  }
  function renderKbRows() {
    const q = KB.q.trim().toLowerCase();
    const rows = KB.entries.filter((e) =>
      (!KB.source || e.source === KB.source) &&
      (!q || [e.source, e.theme, e.kind, e.content].some((v) => String(v || "").toLowerCase().includes(q))));
    $("#kb-tbl tbody").innerHTML = rows.map((e) => {
      const brief = e.content.length > 90 ? e.content.slice(0, 90) + "…" : e.content;
      return `<tr>
        <td><input type="checkbox" class="kbmk" value="${e.id}"></td><td>${e.id}</td>
        <td>${esc(e.source || "-")}</td><td>${esc(e.theme || "-")}</td><td>${esc(e.kind || "-")}</td>
        <td class="kb-content" title="${esc(e.content)}" style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer">${esc(brief)}</td>
        <td class="muted">${fmtTs(e.created_at)}</td></tr>`;
    }).join("") || '<tr><td colspan="7" class="muted">知识库还是空的(默认每天自动采集一条素材)。</td></tr>';
    $$("#kb-tbl tbody tr").forEach((tr) => {
      tr.onclick = (ev) => {
        if (ev.target.tagName === "INPUT") return;
        const entry = KB.entries.find((x) => String(x.id) === tr.querySelector(".kbmk").value);
        if (!entry) return;
        openModal(`📚 ${entry.source || "未知来源"} · ${entry.theme || ""}`,
                  `<pre class="code" style="white-space:pre-wrap">${esc(entry.content)}</pre>`,
                  [{ label: "关闭" }]);
      };
    });
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
      $("#kb-source").onchange = () => { KB.source = $("#kb-source").value; renderKbRows(); };
      $("#kb-search").oninput = () => { KB.q = $("#kb-search").value; renderKbRows(); };
      $("#kb-del").onclick = () => {
        const ids = $$(".kbmk:checked").map((x) => parseInt(x.value, 10));
        if (!ids.length) return toast("未勾选任何条目");
        confirmAction("删除知识库条目", `将删除 ${ids.length} 条素材,不可恢复。`, async () => {
          const r = await apiPost("/admin/api/kb/delete", { gid: GID, ids });
          toast(`✅ 已删除 ${r.deleted} 条`);
          loadKb();
        });
      };
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
