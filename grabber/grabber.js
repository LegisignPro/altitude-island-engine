/*
 * Altitude Map Grabber — runs inside the floor-plan page you are looking at.
 *
 * Click the bookmark while a MapYourShow, A2Z/Personify, EXPOCAD FX or ExpoFP floor plan
 * (or exhibitor list) is open. It reads the same data the page itself loads, joins exhibitors
 * to booths, and downloads one CSV per show in the Island Engine format. Nothing is invented:
 * a size the platform does not publish stays 0 with size_source "unknown".
 *
 * Every platform format here was checked against a live show on 2026-09-23:
 *   MapYourShow  IMTS 2026 (directory.imts.com, white-label)
 *   A2Z          KBIS 2026 (kbis.a2zinc.net)
 *   EXPOCAD FX   Data Center World 2026 (expocad.com/host/fx/informa/26dcw)
 *   ExpoFP       IMEX America 2026 (imexamerica26.expofp.com)
 */
(function () {
  "use strict";
  if (window.__AIG_RUNNING) { return; }
  window.__AIG_RUNNING = true;

  var VERSION = "3.0.0";
  var SQM_TO_SQFT = 10.7639;
  var DETAIL_MIN_SQFT = 400;      // detail pages (website, HQ city/state) only for island-size booths
  var DETAIL_WORKERS = 4;          // polite: at most 4 requests in flight
  var RAW = {};                    // canonical raw payload (same shape the Python normalisers read)
  var EXCLUDE_NAME_RE = /\b(pavilion|association|state of|department)\b/i;

  var COLUMNS = [
    "exhibitor_name", "booth_number", "width", "length", "sqft", "hall", "website",
    "is_sponsor", "has_video_listing", "exhid", "detail_url", "size_source",
    "platform", "booth_count", "shared_booth", "shared_with", "booth_sqft", "raw_size",
    "city", "state", "country", "company_linkedin", "phone", "description", "categories",
    "show_name", "show_year", "source_url", "extracted_at", "grabber_version"
  ];

  // ---------------------------------------------------------------- UI panel
  var panel = document.createElement("div");
  panel.id = "__aig_panel";
  panel.style.cssText = "position:fixed;top:16px;right:16px;z-index:2147483647;width:360px;max-height:80vh;" +
    "overflow:auto;background:#0f172a;color:#e2e8f0;font:13px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;" +
    "border:1px solid #334155;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.45);padding:14px";
  panel.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
    '<b style="font-size:14px">Altitude Map Grabber</b>' +
    '<span id="__aig_x" style="cursor:pointer;color:#94a3b8;font-size:18px">&times;</span></div>' +
    '<div id="__aig_log" style="white-space:pre-wrap;color:#cbd5e1"></div>';
  document.body.appendChild(panel);
  document.getElementById("__aig_x").onclick = function () { panel.remove(); window.__AIG_RUNNING = false; };
  var logEl = document.getElementById("__aig_log");
  function log(msg) { logEl.textContent += msg + "\n"; }

  // ---------------------------------------------------------------- helpers
  function clean(s) { return String(s == null ? "" : s).replace(/\s+/g, " ").trim(); }
  function nameKey(s) { return clean(s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
  function num(s) { var m = String(s == null ? "" : s).replace(/,/g, "").match(/-?\d+(\.\d+)?/); return m ? parseFloat(m[0]) : 0; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function dims(raw) {
    var m = String(raw || "").match(/(\d+(?:\.\d+)?)\s*'?\s*[x×]\s*(\d+(?:\.\d+)?)/i);
    return m ? [parseFloat(m[1]), parseFloat(m[2])] : [null, null];
  }
  function shoelace(pts) {
    var a = 0;
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i], q = pts[(i + 1) % pts.length];
      a += p[0] * q[1] - q[0] * p[1];
    }
    return Math.abs(a) / 2;
  }
  async function getText(url, opts) {
    for (var attempt = 0; attempt < 3; attempt++) {
      var r = await fetch(url, opts || {});
      if (r.status === 429 || r.status === 503) { await sleep(1500 * (attempt + 1)); continue; }
      if (!r.ok) throw new Error("HTTP " + r.status + " for " + url.split("?")[0]);
      return await r.text();
    }
    throw new Error("Server kept answering 429/503 for " + url.split("?")[0]);
  }
  function jsonp(url) {
    return new Promise(function (resolve, reject) {
      var cb = "__aig_cb_" + Math.random().toString(36).slice(2);
      var s = document.createElement("script");
      var t = setTimeout(function () { cleanup(); reject(new Error("JSONP timeout")); }, 30000);
      function cleanup() { clearTimeout(t); try { delete window[cb]; } catch (e) { window[cb] = undefined; } s.remove(); }
      window[cb] = function (d) { cleanup(); resolve(d); };
      s.onerror = function () { cleanup(); reject(new Error("JSONP load error")); };
      s.src = url + (url.indexOf("?") >= 0 ? "&" : "?") + "callback=" + cb;
      document.head.appendChild(s);
    });
  }
  async function pool(items, worker, n, label) {
    var out = new Array(items.length), i = 0, done = 0;
    var prog = null;
    if (label && items.length > 20) { prog = document.createElement("div"); prog.style.color = "#94a3b8"; logEl.appendChild(prog); }
    async function run() { while (i < items.length) { var k = i++; try { out[k] = await worker(items[k], k); } catch (e) { out[k] = null; }
      done++; if (prog && (done % 10 === 0 || done === items.length)) prog.textContent = "  " + label + ": " + done + " / " + items.length; } }
    var runners = []; for (var j = 0; j < Math.min(n, items.length); j++) runners.push(run());
    await Promise.all(runners);
    return out;
  }
  function yearFrom(text) {
    var m = String(text || "").match(/\b(20\d{2})\b/);
    return m ? parseInt(m[1], 10) : null;
  }
  function blankRow() {
    var r = {}; COLUMNS.forEach(function (c) { r[c] = ""; });
    r.sqft = 0; r.booth_count = 0; r.shared_booth = false; r.shared_with = 0; r.booth_sqft = 0;
    r.is_sponsor = false; r.has_video_listing = false; r.size_source = "unknown";
    return r;
  }

  /*
   * Group booth records into one row per company.
   * booth = {key, name, booth, hall, area, raw, w, l, shared (number of companies on this booth), extra...}
   * Shared booths: each company is credited area / companies so a pavilion member never looks like an island.
   * booth_sqft keeps the full stand size for reference.
   */
  function groupBooths(booths, platform, sizeSource) {
    var by = {}, order = [];
    booths.forEach(function (b) {
      var k = b.key || nameKey(b.name);
      if (!k) return;
      if (!by[k]) { by[k] = { name: b.name, list: [] }; order.push(k); }
      by[k].list.push(b);
    });
    return order.map(function (k) {
      var g = by[k], r = blankRow();
      var list = g.list, biggest = list.slice().sort(function (a, b) { return b.area - a.area; })[0];
      var shared = list.some(function (b) { return b.shared > 1; });
      var credited = list.reduce(function (s, b) { return s + (b.area > 0 ? b.area / Math.max(1, b.shared) : 0); }, 0);
      r.exhibitor_name = g.name;
      r.booth_number = list.map(function (b) { return b.booth; }).filter(Boolean).filter(function (v, i, a) { return a.indexOf(v) === i; }).join(", ");
      r.hall = list.map(function (b) { return b.hall; }).filter(Boolean).filter(function (v, i, a) { return a.indexOf(v) === i; }).join("; ");
      r.sqft = Math.round(credited);
      r.booth_sqft = Math.round(list.reduce(function (s, b) { return s + (b.area || 0); }, 0));
      r.booth_count = list.length;
      r.shared_booth = shared;
      r.shared_with = shared ? Math.max.apply(null, list.map(function (b) { return b.shared || 1; })) : 0;
      r.width = biggest && biggest.w ? biggest.w : "";
      r.length = biggest && biggest.l ? biggest.l : "";
      r.raw_size = list.map(function (b) { return b.raw; }).filter(Boolean).join(" + ");
      r.size_source = r.sqft > 0 ? sizeSource : "unknown";
      r.platform = platform;
      var ex = list[0].extra || {};
      Object.keys(ex).forEach(function (c) { if (ex[c] !== undefined && ex[c] !== null) r[c] = ex[c]; });
      return r;
    });
  }

  // ---------------------------------------------------------------- MapYourShow
  function mysRoot() { var m = location.pathname.match(/\/(\d+_\d+)\//); return m ? m[1] : null; }
  async function mysProbe() {
    var root = mysRoot();
    if (!root) return false;
    try {
      var t = await getText("/" + root + "/ajax/remote-proxy.cfm?action=getsearchoptions&function=getBoothHalls",
        { headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" } });
      var d = JSON.parse(t); return Array.isArray(d.DATA);
    } catch (e) { return false; }
  }
  async function extractMYS() {
    var root = mysRoot() || "8_0";
    var H = { headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" } };
    var proxy = "/" + root + "/ajax/remote-proxy.cfm";
    var halls = {};
    JSON.parse(await getText(proxy + "?action=getsearchoptions&function=getBoothHalls", H)).DATA.forEach(function (h) {
      if (h.fieldvalue) halls[String(h.fieldvalue)] = clean(h.fielddisplay || h.fieldvalue);
    });
    log("Halls: " + Object.keys(halls).length);
    RAW.halls = halls;
    var gal = JSON.parse(await getText(proxy + "?action=search&searchtype=exhibitorgallery&searchsize=20000", H));
    var hits = (((gal.DATA || {}).results || {}).exhibitor || {}).hit || [];
    log("Exhibitor list: " + hits.length);
    var gallery = {};
    RAW.gallery = hits.map(function (h) { var f = h.fields || {}; return { id: h.id, fields: { exhid_l: f.exhid_l, exhname_t: f.exhname_t, boothsdisplay_la: f.boothsdisplay_la, booths_la: f.booths_la, hallid_la: f.hallid_la, exhfeatured_t: f.exhfeatured_t, exhdesc_t: clean(f.exhdesc_t).slice(0, 200), exhtags_la: f.exhtags_la } }; });
    hits.forEach(function (h) {
      var f = h.fields || {}, id = String(f.exhid_l || h.id || "").trim(), name = clean(f.exhname_t);
      if (!id || !name) return;
      var flag = function (needles) {
        return Object.keys(f).some(function (k) {
          var v = f[k], kl = k.toLowerCase();
          if (!needles.some(function (n) { return kl.indexOf(n) >= 0; })) return false;
          if (typeof v === "string") return ["1", "true", "yes", "y", "t"].indexOf(v.trim().toLowerCase()) >= 0;
          if (Array.isArray(v)) return v.length > 0;
          return !!v;
        });
      };
      gallery[id] = {
        name: name, halls: (f.hallid_la || []).map(String),
        booth: (f.boothsdisplay_la || f.booths_la || []).map(function (b) { return clean(b).replace(/randomstring$/i, ""); }).filter(Boolean).join(", "),
        sponsor: flag(["sponsor", "featured", "premium"]), video: flag(["video"]),
        description: clean(f.exhdesc_t).slice(0, 500),
        categories: (f.exhtags_la || []).map(clean).join("; ")
      };
    });
    var fpHtml = await getText("/" + root + "/floorplan/");
    var showid = (fpHtml.match(/ShowID\s*=\s*"([^"]+)"/) || [])[1] || location.host.split(".")[0].toUpperCase();
    var fpver = (fpHtml.match(/floorplan\/(\d{2})\//) || [])[1] || "02";
    var wanted = Object.keys(halls);
    var booths = [];
    var versions = ["02"].concat(fpver !== "02" ? [fpver] : []);
    for (var vi = 0; vi < versions.length && !booths.length; vi++) {
      var ver = versions[vi];
      var perHall = await pool(wanted, async function (hall) {
        var d = JSON.parse(await getText("/" + root + "/floorplan/" + ver + "/_remote-proxy.cfm?showid=" + encodeURIComponent(showid) +
          "&selectedbooth=&hallid=" + encodeURIComponent(hall) + "&action=GetBoothByHall&method=GetBoothByHall&regid=0", H));
        var cols = d.COLUMNS || [], out = [];
        (RAW.hall_booths = RAW.hall_booths || {})[hall] = { COLUMNS: cols, DATA: (d.DATA || []).map(function (raw) { return raw.map(function (v, i) { if (cols[i] !== "FEATUREPROPERTIES") return v; var p = v; if (typeof p === "string") { try { p = JSON.parse(p); } catch (e) { p = {}; } } return { properties: (p || {}).properties || {} }; }); }) };
        (d.DATA || []).forEach(function (raw) {
          var rec = {}; cols.forEach(function (c, i) { rec[c] = raw[i]; });
          if (rec.OBJECTTYPE !== "booth" || !rec.EXHID) return;
          var props = rec.FEATUREPROPERTIES || {};
          if (typeof props === "string") { try { props = JSON.parse(props); } catch (e) { props = {}; } }
          props = props.properties || {};
          var w = props.boothWidth ? Math.round(num(props.boothWidth) / 12 * 10) / 10 : null;
          var l = props.boothHeight ? Math.round(num(props.boothHeight) / 12 * 10) / 10 : null;
          var area = num(props.area) || (w && l ? w * l : 0);
          out.push({ exhid: String(rec.EXHID), name: clean(rec.EXHNAME), booth: clean(rec.BOOTHDISPLAY || rec.BOOTH),
            hall: halls[hall] || hall, area: Math.round(area), w: w, l: l, raw: w && l ? w + "' x " + l + "'" : (area ? area + " sq ft" : "") });
        });
        return out;
      }, DETAIL_WORKERS);
      perHall.forEach(function (list) { if (list) booths = booths.concat(list); });
    }
    log("Floor-plan booths: " + booths.length);
    RAW.showid = showid;
    var byExh = {};
    booths.forEach(function (b) { (byExh[b.exhid] = byExh[b.exhid] || []).push(b); });
    var boothRecs = [];
    Object.keys(gallery).forEach(function (id) {
      var g = gallery[id], list = byExh[id] || [];
      var extra = { exhid: id, is_sponsor: g.sponsor, has_video_listing: g.video, description: g.description, categories: g.categories,
        detail_url: location.origin + "/" + root + "/exhibitor/exhibitor-details.cfm?exhid=" + id };
      if (!list.length) {
        boothRecs.push({ key: "id:" + id, name: g.name, booth: g.booth, hall: g.halls.map(function (h) { return halls[h] || h; }).join("; "), area: 0, shared: 1, extra: extra });
      } else {
        list.forEach(function (b) { boothRecs.push({ key: "id:" + id, name: g.name, booth: b.booth, hall: b.hall, area: b.area, w: b.w, l: b.l, raw: b.raw, shared: 1, extra: extra }); });
      }
    });
    Object.keys(byExh).forEach(function (id) {
      if (gallery[id]) return;
      var list = byExh[id], nm = (list.find(function (b) { return b.name && b.name.toLowerCase() !== "unassigned"; }) || {}).name;
      if (!nm) return;
      list.forEach(function (b) { boothRecs.push({ key: "id:" + id, name: nm, booth: b.booth, hall: b.hall, area: b.area, w: b.w, l: b.l, raw: b.raw, shared: 1,
        extra: { exhid: id, detail_url: location.origin + "/" + root + "/exhibitor/exhibitor-details.cfm?exhid=" + id } }); });
    });
    var rows = groupBooths(boothRecs, "mapyourshow", "floorplan");
    // Detail pages: website, LinkedIn, phone, HQ city/state/country -- only for booths worth a look.
    var todo = rows.filter(function (r) { return r.sqft >= DETAIL_MIN_SQFT && r.exhid; });
    log("Reading " + todo.length + " exhibitor detail pages (" + DETAIL_MIN_SQFT + "+ sq ft)...");
    RAW.details = {};
    await pool(todo, async function (r) {
      var h = await getText("/" + root + "/exhibitor/exhibitor-details.cfm?exhid=" + encodeURIComponent(r.exhid));
      var get = function (k) { var m = h.match(new RegExp(k + '\\s*:\\s*"([^"]*)"')); return m ? m[1].replace(/\\\//g, "/").trim() : ""; };
      var site = get("websiteValue"); if (site && !/^https?:/i.test(site)) site = "https://" + site;
      r.website = site; r.company_linkedin = get("linkedInValue"); r.phone = get("phoneValue");
      if (/(videoValue|videoUrl|youtubeValue|vimeoValue)\s*:\s*"[^"]+"/i.test(h)) r.has_video_listing = true;
      var am = h.match(/addressValues\s*:\s*(\{[^}]*\})/);
      RAW.details[r.exhid] = { website: r.website, linkedin: r.company_linkedin, phone: r.phone, address: am ? am[1] : "" };
      if (am) { try { var a = JSON.parse(am[1]); r.city = clean(a.CITY); r.state = clean(a.STATE); r.country = clean(a.COUNTRY); } catch (e) { /* leave blank */ } }
    }, DETAIL_WORKERS, "detail pages");
    var yr = showid.match(/(\d{2,4})$/);
    return { rows: rows, meta: {
      platform: "mapyourshow", show_name: clean(document.title.split("|")[0]).replace(/\s*\b20\d{2}\b\s*$/, ""),
      show_year: yr ? (yr[1].length === 2 ? 2000 + parseInt(yr[1], 10) : parseInt(yr[1], 10)) : null,
      showid: showid, platform_count: Object.keys(gallery).length } };
  }

  // ---------------------------------------------------------------- A2Z / Personify
  async function extractA2Z() {
    var html = document.documentElement.outerHTML;
    if (!/EventMap\.aspx/i.test(location.pathname)) {
      // The exhibitor list page doesn't carry map ids; load the event map from the same folder.
      html = await getText(location.pathname.replace(/[^/]+$/, "") + "EventMap.aspx?shMode=E");
    }
    var ev = (html.match(/intRootEventID\s*=\s*['"]?(\d+)/) || [])[1];
    var app = (html.match(/strRootApplicationID\s*=\s*['"]([^'"]+)/) || [])[1];
    var maps = []; (html.match(/data-mapId=["']\d+["']/gi) || []).forEach(function (m) { var id = m.match(/\d+/)[0]; if (maps.indexOf(id) < 0) maps.push(id); });
    var base = ((html.match(/customTileBaseUrl\s*=\s*['"]([^'"]+)['"]/) || [])[1] || "https://img14.a2zinc.net").replace(/\/+$/, "");
    if (!ev || !app || !maps.length) throw new Error("This A2Z page has no event map ids. Open the show's EventMap.aspx floor plan and click again.");
    log("A2Z event " + ev + ", " + maps.length + " map(s)");
    var labelCount = document.querySelectorAll("a.boothLabel").length ||
      (new DOMParser().parseFromString(html, "text/html")).querySelectorAll("a.boothLabel").length;
    var records = [];
    for (var i = 0; i < maps.length; i++) {
      var q = "mapId=" + maps[i] + "&eventId=" + ev + "&appId=" + encodeURIComponent(app) + "&floorplanViewType=View4&langId=1&boothId=&shMode=E";
      var d = await jsonp(base + "/api/exhibitor?" + q);
      log("  map " + maps[i] + ": " + (d || []).length + " booths");
      (d || []).forEach(function (b) { b.__map = maps[i]; records.push(b); });
      await sleep(300);
    }
    RAW.event_id = ev; RAW.map_ids = maps; RAW.label_count = labelCount;
    RAW.records = records.map(function (b) { var c = {}; ["name", "enhanced", "label", "id", "mapId", "status", "size", "dimension", "unit", "coExhibitors", "coExhs", "videoCount", "hyperLinkFieldValue", "boothName"].forEach(function (k) { c[k] = b[k]; }); return c; });
    var boothRecs = [];
    records.forEach(function (b) {
      var name = clean(b.name);
      if (!name || b.status === 0) return;
      var unit = String(b.unit || "sq ft").toLowerCase();
      var sqm = /m/.test(unit) && !/ft/.test(unit);
      var area = num(b.size) * (sqm ? SQM_TO_SQFT : 1);
      var wl = dims(b.dimension);
      var co = (b.coExhs && b.coExhs.length) ? b.coExhs.length : 0;
      boothRecs.push({ name: name, booth: clean((b.label && b.label.text) || b.boothName), hall: "", area: Math.round(area),
        w: wl[0], l: wl[1], raw: clean(b.dimension) || (b.size ? b.size + " " + (b.unit || "") : ""), shared: 1 + co,
        extra: { exhid: String(b.hyperLinkFieldValue || b.id || ""), has_video_listing: (b.videoCount || 0) > 0, is_sponsor: !!b.enhanced,
          detail_url: location.origin + location.pathname.replace(/[^/]+$/, "") + "eBooth.aspx?BoothID=" + (b.hyperLinkFieldValue || b.id) },
        sqm: sqm });
    });
    var rows = groupBooths(boothRecs, "a2z", boothRecs.some(function (b) { return b.sqm; }) ? "a2z-map-sqm" : "a2z-map");
    var todo = rows.filter(function (r) { return r.sqft >= DETAIL_MIN_SQFT && r.detail_url; });
    log("Reading " + todo.length + " booth pages (" + DETAIL_MIN_SQFT + "+ sq ft)...");
    await pool(todo, async function (r) {
      var html = await getText(r.detail_url);
      var t = function (c) {
        var m = html.match(new RegExp('class="' + c + '"[^>]*>([\\s\\S]*?)</'));
        return m ? clean(m[1].replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&")).replace(/,$/, "") : "";
      };
      r.city = t("BoothContactCity"); r.state = t("BoothContactState"); r.country = t("BoothContactCountry");
      var site = t("BoothContactUrl"); if (site && !/^https?:/i.test(site)) site = "https://" + site;
      r.website = site;
      (RAW.details = RAW.details || {})[r.exhid] = { city: r.city, state: r.state, country: r.country, website: r.website };
    }, DETAIL_WORKERS, "booth pages");
    return { rows: rows, meta: { platform: "a2z", show_name: clean(document.title.replace(/-\s*Event Map.*/i, "").replace(/\b20\d{2}\b/, "")),
      show_year: yearFrom(document.title) || yearFrom(location.pathname), platform_count: labelCount, booth_records: records.length } };
  }

  // ---------------------------------------------------------------- EXPOCAD FX
  async function extractEXPOCAD() {
    for (var i = 0; i < 45 && !(window.data && window.data.booths && window.data.booths.length); i++) await sleep(1000);
    var D = window.data;
    if (!D || !D.booths || !D.booths.length) throw new Error("EXPOCAD did not finish loading its floor plan (waited 45 s).");
    var ex = D.exhibitors || [];
    log("EXPOCAD booths: " + D.booths.length + ", exhibitor records: " + ex.length);
    RAW.booths = D.booths.map(function (b) { return { number: b.number, status: b.status, exhibitorIndex: b.exhibitorIndex, areaF: b.areaF, areaM: b.areaM, dimF: b.dimF }; });
    RAW.exhibitors = ex.map(function (e) { return { name: e.name, exhId: e.exhId, id: e.id, website: e.website, city: e.city, state: e.state, country: e.country, phone: e.phone, category: e.category, profile: clean(e.profile).slice(0, 200) }; });
    var boothRecs = [];
    D.booths.forEach(function (b) {
      var e = ex[parseInt(b.exhibitorIndex, 10)];
      if (!e || !clean(e.name) || String(b.status) === "0") return;
      var area = num(b.areaF), wl = dims(b.dimF);
      if (!area && b.areaM) area = num(b.areaM) * SQM_TO_SQFT;
      boothRecs.push({ key: "id:" + (e.exhId || e.id || nameKey(e.name)), name: clean(e.name), booth: clean(b.number), hall: "",
        area: Math.round(area), w: wl[0], l: wl[1], raw: clean(b.dimF || b.areaF), shared: 1,
        extra: { exhid: String(e.exhId || e.id || ""), website: clean(e.website), city: clean(e.city), state: clean(e.state),
          country: clean(e.country), phone: clean(e.phone), description: clean(e.profile).slice(0, 500), categories: clean(e.category) } });
    });
    var title = "";
    try {
      var code = location.pathname.split("/").filter(Boolean).slice(-2, -1)[0];
      var cfg = await getText(location.pathname.replace(/[^/]+$/, "") + "config_" + code + ".xml");
      title = (cfg.match(/\beT="([^"]+)"/) || [])[1] || "";
    } catch (e) { title = ""; }
    title = title || clean(document.title);
    RAW.config_title = title;
    return { rows: groupBooths(boothRecs, "expocad", "expocad-fx"), meta: { platform: "expocad",
      show_name: clean(title.replace(/\b20\d{2}\b/, "")), show_year: yearFrom(title),
      platform_count: ex.filter(function (e) { return clean(e.name); }).length } };
  }

  // ---------------------------------------------------------------- ExpoFP
  function fpTemplate(js) { var a = js.indexOf("`"), b = js.lastIndexOf("`"); return a >= 0 && b > a ? js.slice(a + 1, b) : ""; }
  function fpPaths(js) {
    var m = js.match(/window\['__fpPaths[^']*'\]\s*=\s*(\[[\s\S]*?\]);\s*\r?\n/);
    try { return m ? JSON.parse(m[1]) : []; } catch (e) { return []; }
  }
  async function extractExpoFP() {
    if (/(^|\.)expofp\.com$/i.test(location.host) && location.host.split(".").length === 2)
      throw new Error("This is an ExpoFP calendar/marketing page, not a floor plan. Open the show's own <name>.expofp.com map.");
    var t = await getText("/data/data.js");
    var d = JSON.parse(t.slice(t.indexOf("{"), t.lastIndexOf("}") + 1));
    var base = await getText("/data/fp.svg.js");
    var layers = [];
    var lm = base.match(/__fpLayers\s*=\s*(\[[\s\S]*?\]);\s*\r?\n/);
    try { layers = lm ? JSON.parse(lm[1]).map(function (l) { return l.name; }) : []; } catch (e) { layers = []; }
    var files = [{ name: "(default)", js: base }];
    for (var i = 0; i < layers.length; i++) {
      try { files.push({ name: layers[i], js: await getText("/data/fp.svg." + encodeURIComponent(layers[i]) + ".js") }); }
      catch (e) { /* the default layer lives in fp.svg.js itself, so a 404 here is expected once */ }
    }
    log("ExpoFP layers read: " + files.length);
    var area = {}, unitsFt = true;
    RAW.layers = [];
    files.forEach(function (f) {
      var svgTxt = fpTemplate(f.js); if (!svgTxt) return;
      var doc = new DOMParser().parseFromString(svgTxt, "image/svg+xml");
      var u = doc.documentElement.getAttribute("units"); if (u && u !== "ft") unitsFt = false;
      var mult = u === "m" ? SQM_TO_SQFT : 1;
      var paths = fpPaths(f.js);
      var rl = { name: f.name, units: u || "", shapes: [] }; RAW.layers.push(rl);
      doc.querySelectorAll('[id^="b"]').forEach(function (el) {
        var id = el.id.slice(1), a = 0;
        if (el.tagName === "rect") a = num(el.getAttribute("width")) * num(el.getAttribute("height"));
        else {
          var p = el.querySelector("path[data-index]");
          var geo = p ? paths[parseInt(p.getAttribute("data-index"), 10)] : null;
          if (geo && geo.positions && geo.positions.length > 2) a = shoelace(geo.positions);
        }
        if (el.tagName === "rect") rl.shapes.push({ id: el.id, tag: "rect", width: num(el.getAttribute("width")), height: num(el.getAttribute("height")) });
        else { var pp = el.querySelector("path[data-index]"); var gg = pp ? paths[parseInt(pp.getAttribute("data-index"), 10)] : null; rl.shapes.push({ id: el.id, tag: el.tagName, positions: gg && gg.positions ? gg.positions : [] }); }
        if (a > 0 && !area[id]) area[id] = a * mult;
      });
    });
    RAW.data = { title: d.title, startDate: d.startDate, booths: (d.booths || []).map(function (b) { return { id: b.id, name: b.name, special: !!b.special, exhibitors: b.exhibitors || [] }; }), exhibitors: (d.exhibitors || []).map(function (e) { return { id: e.id, name: e.name, featured: !!e.featured, description: clean(String(e.description || "").replace(/<[^>]+>/g, " ")).slice(0, 200) }; }) };
    var exById = {}; (d.exhibitors || []).forEach(function (e) { exById[e.id] = e; });
    var boothRecs = [], linked = {};
    (d.booths || []).forEach(function (b) {
      if (b.special || !b.exhibitors || !b.exhibitors.length) return;
      var ids = b.exhibitors.filter(function (id) { return exById[id] && clean(exById[id].name); });
      ids.forEach(function (id) {
        linked[nameKey(exById[id].name)] = 1;
        var e = exById[id], a = area[b.name] || 0;
        boothRecs.push({ key: nameKey(e.name), name: clean(e.name), booth: clean(b.name), hall: "", area: Math.round(a),
          raw: a ? Math.round(a) + " sq ft (from floor-plan shape)" : "", shared: ids.length,
          extra: { exhid: String(id), is_sponsor: !!e.featured, description: clean((e.description || "").replace(/<[^>]+>/g, " ")).slice(0, 500) } });
      });
    });
    return { rows: groupBooths(boothRecs, "expofp", unitsFt ? "expofp-svg" : "expofp-svg-sqm"), meta: { platform: "expofp",
      show_name: clean(String(d.title || "").replace(/\b20\d{2}\b/, "")), show_year: yearFrom(d.startDate) || yearFrom(d.title),
      platform_count: Object.keys(linked).length, start_date: d.startDate || "" } };
  }

  // ---------------------------------------------------------------- router
  async function detect() {
    var host = location.host.toLowerCase(), path = location.pathname;
    if (/expofp\.com$/.test(host) || window.__data || window.__fp) return "expofp";
    if (/a2zinc\.net$|mya2zevents\.com$/.test(host) || /\/Public\/(EventMap|Exhibitors)\.aspx/i.test(path)) return "a2z";
    if (/expocad(web)?\.com$/.test(host) || /exfx\.html$/i.test(path) || document.querySelector('script[src*="expofx"]') || (window.data && window.data.booths)) return "expocad";
    if (/mapyourshow\.com$/.test(host) || await mysProbe()) return "mapyourshow";
    return null;
  }

  // ---------------------------------------------------------------- quality (same thresholds as quality.py)
  function quality(rows, meta) {
    var f = [], worst = "PASS";
    function add(level, check, note) { f.push(level + " " + check + ": " + note); if (level === "FAIL" || (level === "WARN" && worst === "PASS")) worst = level; }
    var n = rows.length, sized = rows.filter(function (r) { return r.sqft > 0; }).length;
    if (n < 10) add("FAIL", "Row count", n + " companies"); else if (n < 50) add("WARN", "Row count", n + " companies");
    var share = n ? sized / n : 0;
    if (share < 0.4) add("FAIL", "Sized share", Math.round(share * 100) + "% have a booth size");
    else if (share < 0.7) add("WARN", "Sized share", Math.round(share * 100) + "% have a booth size");
    if (meta.platform_count) {
      var cov = n / meta.platform_count;
      if (cov < 0.8) add("FAIL", "Coverage", n + " of " + meta.platform_count + " listed exhibitors");
      else if (cov < 0.95) add("WARN", "Coverage", n + " of " + meta.platform_count + " listed exhibitors");
    }
    var big = rows.filter(function (r) { return r.booth_count === 1 && r.booth_sqft > 50000; });
    if (big.length) add("FAIL", "Implausible sizes", big.length + " single booths over 50,000 sq ft");
    if (n >= 200 && !rows.some(function (r) { return r.sqft >= 400; })) add("WARN", "Island sanity", "no company at 400+ sq ft");
    if (worst === "PASS") f.push("PASS all checks");
    return { grade: worst, notes: f };
  }

  function toCSV(rows) {
    var esc = function (v) { v = v === null || v === undefined ? "" : String(v); return /[",\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
    return [COLUMNS.join(",")].concat(rows.map(function (r) { return COLUMNS.map(function (c) { return esc(r[c]); }).join(","); })).join("\r\n");
  }
  function button(label, onclick) {
    var b = document.createElement("button");
    b.textContent = label;
    b.style.cssText = "display:block;margin-top:8px;padding:7px 12px;border-radius:6px;border:0;background:#1f6fb5;" +
      "color:#fff;cursor:pointer;font:600 13px -apple-system,Segoe UI,Roboto,sans-serif";
    b.onclick = onclick;
    panel.appendChild(b);
    return b;
  }
  function download(name, text) {
    var blob = new Blob(["\ufeff" + text], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 2000);
  }

  // ---------------------------------------------------------------- main
  (async function main() {
    try {
      log("v" + VERSION + " — detecting platform...");
      var platform = await detect();
      if (!platform) throw new Error("Not a supported floor plan. Supported: MapYourShow, A2Z/Personify, EXPOCAD FX, ExpoFP.");
      log("Platform: " + platform);
      var res = await ({ mapyourshow: extractMYS, a2z: extractA2Z, expocad: extractEXPOCAD, expofp: extractExpoFP })[platform]();
      var before = res.rows.length;
      var rows = res.rows.filter(function (r) { return !EXCLUDE_NAME_RE.test(r.exhibitor_name); });
      var now = new Date().toISOString();
      rows.forEach(function (r) {
        r.show_name = res.meta.show_name || ""; r.show_year = res.meta.show_year || "";
        r.source_url = location.origin + location.pathname; r.extracted_at = now; r.grabber_version = VERSION;
      });
      rows.sort(function (a, b) { return b.sqft - a.sqft || a.exhibitor_name.localeCompare(b.exhibitor_name); });
      var q = quality(rows, res.meta);
      var islands = rows.filter(function (r) { return r.sqft >= 400; }).length;
      var shared = rows.filter(function (r) { return r.shared_booth; }).length;
      log("Name filter removed " + (before - rows.length) + " pavilion/association/government listings");
      log("Companies: " + rows.length + "  |  sized: " + rows.filter(function (r) { return r.sqft > 0; }).length +
        "  |  400+ sq ft: " + islands + "  |  on shared booths: " + shared);
      log("Show: " + (res.meta.show_name || "?") + "  year: " + (res.meta.show_year || "not stated — set it in the app"));
      log("Quality: " + q.grade + "\n  " + q.notes.join("\n  "));
      window.__AIG_RESULT = { rows: rows, meta: res.meta, quality: q, raw: RAW };
      if (window.__AIG_DEBUG) {
        var rawName = (res.meta.show_name || location.host.split(".")[0]).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") + "_" + (res.meta.show_year || "year-unknown") + "_" + platform + "_raw.json";
        // One file only: browsers block a second automatic download from the same click.
        var blob = new Blob([JSON.stringify({ platform: platform, url: location.origin + location.pathname, captured_at: new Date().toISOString(),
          meta: res.meta, quality: q, raw: RAW, csv: toCSV(rows) })], { type: "application/json" });
        var saveRaw = function () { var a2 = document.createElement("a"); a2.href = URL.createObjectURL(blob); a2.download = rawName; document.body.appendChild(a2); a2.click(); };
        saveRaw();
        button("Save debug file again", saveRaw).id = "__aig_raw_btn";
        log("Debug: raw payload + CSV saved together as " + rawName);
        return;
      }
      if (q.grade === "FAIL") { log("\nNot downloaded: quality FAIL. Nothing here is safe to compare."); return; }
      var slug = (res.meta.show_name || location.host.split(".")[0]).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
      var fname = slug + "_" + (res.meta.show_year || "year-unknown") + "_" + platform + ".csv";
      var csvText = toCSV(rows);
      download(fname, csvText);
      button("Download CSV again", function () { download(fname, csvText); }).id = "__aig_csv_btn";
      log("\nDownloaded " + fname + " — load it in the Island Engine sidebar (Map Grabber CSVs). If your browser " +
          "blocked the download, use the button below.");
    } catch (e) {
      log("\nStopped: " + (e && e.message ? e.message : e));
    } finally {
      window.__AIG_RUNNING = false;
    }
  })();
})();
