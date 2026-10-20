/*
 * claude-shim.js — Claude artifact çalışma zamanı arayüzünün bağımsız sürümü.
 *
 * index.html, Claude artifact'i olarak yazıldı ve şu arayüzleri kullanır:
 *   claude.use("db")        -> collection(..).orderBy(..).onSnapshot / add, doc(..).set / delete / onSnapshot
 *   claude.use("user")      -> can("data.write")
 *   claude.use("downloads") -> save({filename, data})
 * Bu dosya aynı arayüzü backend REST API'si (/api/...) ve SSE değişiklik akışı (/api/events)
 * üzerinden sağlar. Böylece index.html değiştirilmeden çalışır.
 */
(function () {
  "use strict";
  const API = (window.PORTFOY_API_BASE || "") + "/api";

  async function req(method, path, body) {
    const r = await fetch(API + path, {
      method,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (r.status === 404 && method === "GET") return null;
    if (!r.ok) {
      let d = {};
      try { d = await r.json(); } catch (_) {}
      const e = new Error(d.message || ("HTTP " + r.status));
      e.code = d.code || (r.status === 403 ? "permission_denied" : r.status === 401 ? "unauthenticated" : "unavailable");
      e.status = r.status;
      throw e;
    }
    return r.json();
  }

  const clone = (o) => (o == null ? o : JSON.parse(JSON.stringify(o)));
  const mkDoc = (row) => ({ id: row.id, exists: true, data: () => clone(row.data), _version: row.version });
  const enc = encodeURIComponent;

  // ---------------------------------------------------------------- canlı abonelikler
  const listeners = new Set(); // {coll, id?, orderBy?, cb, err, timer}

  function refresh(l) {
    clearTimeout(l.timer);
    l.timer = setTimeout(async () => {
      try {
        if (l.id) {
          const row = await req("GET", "/docs/" + enc(l.coll) + "/" + enc(l.id));
          if (!listeners.has(l)) return;
          l.cb(row ? mkDoc(row) : { id: l.id, exists: false, data: () => undefined });
        } else {
          const rows = await req("GET", "/collections/" + enc(l.coll) + (l.orderBy ? "?order_by=" + enc(l.orderBy) : ""));
          if (!listeners.has(l)) return;
          const docs = (rows || []).map(mkDoc);
          l.cb({ docs, size: docs.length, empty: docs.length === 0, forEach: (f) => docs.forEach(f) });
        }
      } catch (e) {
        if (l.err) l.err(e); else console.error("[portfoy] snapshot", e);
      }
    }, 60);
  }

  function touched(coll, id) {
    listeners.forEach((l) => { if (l.coll === coll && (!l.id || !id || l.id === id)) refresh(l); });
  }

  let es = null, opened = false;
  function ensureStream() {
    if (es) return;
    es = new EventSource(API + "/events", { withCredentials: true });
    es.addEventListener("ready", () => {
      // ilk bağlantıda değil, yeniden bağlanmada kaçırılmış değişiklikleri topla
      if (opened) listeners.forEach(refresh);
      opened = true;
    });
    es.addEventListener("change", (ev) => {
      try { const m = JSON.parse(ev.data); touched(m.collection, m.id); } catch (_) {}
    });
  }

  function subscribe(l) {
    listeners.add(l);
    ensureStream();
    refresh(l);
    return () => { listeners.delete(l); clearTimeout(l.timer); };
  }

  // ---------------------------------------------------------------- db
  function query(coll, orderBy) {
    return {
      orderBy: (field) => query(coll, field),
      onSnapshot: (cb, err) => subscribe({ coll, orderBy, cb, err }),
      get: async () => {
        const rows = await req("GET", "/collections/" + enc(coll) + (orderBy ? "?order_by=" + enc(orderBy) : ""));
        const docs = (rows || []).map(mkDoc);
        return { docs, size: docs.length, empty: !docs.length, forEach: (f) => docs.forEach(f) };
      },
    };
  }

  function collection(coll) {
    const q = query(coll);
    q.add = async (data) => {
      const row = await req("POST", "/collections/" + enc(coll), data);
      touched(coll);
      return { id: row.id, path: coll + "/" + row.id };
    };
    q.doc = (id) => docRef(coll + "/" + id);
    return q;
  }

  function docRef(path) {
    const i = path.lastIndexOf("/");
    const coll = path.slice(0, i), id = path.slice(i + 1);
    return {
      id, path,
      set: async (data) => { const r = await req("PUT", "/docs/" + enc(coll) + "/" + enc(id), data); touched(coll, id); return r; },
      update: async (patch) => {
        const cur = await req("GET", "/docs/" + enc(coll) + "/" + enc(id));
        const r = await req("PUT", "/docs/" + enc(coll) + "/" + enc(id), Object.assign({}, cur ? cur.data : {}, patch));
        touched(coll, id); return r;
      },
      delete: async () => { const r = await req("DELETE", "/docs/" + enc(coll) + "/" + enc(id)); touched(coll, id); return r; },
      get: async () => { const row = await req("GET", "/docs/" + enc(coll) + "/" + enc(id)); return row ? mkDoc(row) : { id, exists: false, data: () => undefined }; },
      onSnapshot: (cb, err) => subscribe({ coll, id, cb, err }),
    };
  }

  const db = { collection, doc: docRef };

  // ---------------------------------------------------------------- user
  let meP = null;
  const me = () => (meP = meP || req("GET", "/me"));
  const user = {
    get id() { return null; },
    async info() { return me(); },
    async can(perm) { const m = await me(); return perm === "data.write" ? !!m.can_write : true; },
  };

  // ---------------------------------------------------------------- downloads
  const downloads = {
    async save({ filename, data, mimeType }) {
      let blob = data;
      if (!(blob instanceof Blob)) {
        blob = new Blob([typeof data === "string" || data instanceof ArrayBuffer || ArrayBuffer.isView(data) ? data : JSON.stringify(data)],
          { type: mimeType || "application/octet-stream" });
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename || "dosya"; a.rel = "noopener";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      return { ok: true };
    },
  };

  const caps = { db, user, downloads };
  window.claude = Object.assign(window.claude || {}, {
    use: async (name) => {
      if (name === "user") { try { await me(); } catch (_) { return null; } }
      return caps[name] || null;
    },
  });
})();
