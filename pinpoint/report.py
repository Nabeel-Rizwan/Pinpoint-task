"""Reporting: the dashboard shell + CSV/JSON exports.

The dashboard is a thin, dependency-free client of the JSON API in
:mod:`pinpoint.server`. This module writes the static *shell* (``index.html``) and
the flat exports (``partners.csv``, ``changes.csv``, ``data.json``). The shell
fetches only the page it is showing, so the view is always current and nothing
is pre-embedded or frozen at report time — serve it with ``pinpoint serve``.

The shell is a **product** surface (compare + monitor): dense tables, facet
filters, and a change feed. It is built mobile-first with 16px form controls on
narrow screens (no iOS zoom), 44px touch targets, a horizontal-scroll strategy
for data tables, logical focus states, and a focus-trapping dialog. Untrusted
values reach the DOM through escaping, and profile URLs are scheme-allow-listed
here (in the exports) and again in the client.
"""

from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .change import firm_status
from .models import Firm
from .normalize import safe_url
from .storage import Store

# --- HTML shell --------------------------------------------------------------
# Uses __TOKEN__ placeholders (not str.format) so CSS/JS braces stay untouched.
_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>Pinpoint &middot; Corporate &amp; M&amp;A Partner Intelligence</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a; --line2:#39415a;
          --text:#e7eaf0; --muted:#9aa3b2; --accent:#5b8cff; --accent-d:#1c2740;
          --good:#3ecf8e; --bad:#ff6b6b; --warn:#ffb84d; --r:10px; }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { margin:0; background:var(--bg); color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; }
  h1 { margin:0; font-size:18px; letter-spacing:.2px; }
  h2 { font-size:15px; margin:0 0 12px; }
  h3 { font-size:12px; margin:18px 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px; }
  .sub { color:var(--muted); font-size:12px; margin-top:3px; }
  .muted { color:var(--muted); } .small { font-size:12px; } .mt { margin-top:36px; }
  a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
  :focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:6px; }
  table { font-variant-numeric:tabular-nums; }

  .topbar { position:sticky; top:0; z-index:20; background:rgba(15,17,21,.92);
    -webkit-backdrop-filter:blur(8px); backdrop-filter:blur(8px); border-bottom:1px solid var(--line);
    padding:calc(14px + env(safe-area-inset-top)) 28px 14px;
    display:flex; justify-content:space-between; align-items:flex-end; gap:16px; }
  .brand { min-width:0; }
  .tabs { display:flex; gap:6px; overflow-x:auto; scrollbar-width:none; }
  .tabs::-webkit-scrollbar { display:none; }
  .tab { display:inline-flex; align-items:center; gap:6px; white-space:nowrap; min-height:38px;
    padding:8px 14px; border-radius:999px; border:1px solid var(--line);
    background:var(--panel); color:var(--muted); cursor:pointer; font-size:13px; font-weight:600; }
  .tab:hover { border-color:var(--line2); color:var(--text); }
  .tab.active { color:var(--text); border-color:var(--accent); background:var(--accent-d); }
  .tab .count { color:var(--muted); font-weight:400; }
  .tab.active .count { color:var(--text); }

  .summary { display:flex; gap:12px; flex-wrap:wrap; padding:16px 28px 0; }
  .chip { background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
    padding:10px 14px; min-width:120px; flex:0 0 auto; }
  .chip .n { font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; }
  .chip .l { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.6px; margin-top:2px; }

  .wrap { padding:8px 28px calc(56px + env(safe-area-inset-bottom)); }
  .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin:16px 0; }
  select, .toolbar input[type=search] { padding:9px 11px; border-radius:var(--r);
    border:1px solid var(--line); background:var(--panel); color:var(--text); font-size:13px; }
  select:hover { border-color:var(--line2); }
  .toolbar input[type=search] { min-width:220px; }
  .btn { padding:9px 14px; border-radius:var(--r); border:1px solid var(--line);
    background:var(--panel); color:var(--text); cursor:pointer; font-size:13px; min-height:38px; }
  .btn:hover { border-color:var(--accent); }
  .btn:disabled { opacity:.4; cursor:default; border-color:var(--line); }
  .seg { color:var(--muted); font-size:13px; display:inline-flex; align-items:center; gap:5px; min-height:38px; }
  .seg input { accent-color:var(--accent); }
  .stat { color:var(--muted); font-size:12px; margin-left:auto; }
  .pager { display:flex; gap:8px; align-items:center; margin:14px 0; color:var(--muted); flex-wrap:wrap; }
  code { background:var(--panel2); padding:2px 6px; border-radius:6px; font-size:12px; }

  .table-wrap { overflow-x:auto; -webkit-overflow-scrolling:touch;
    border:1px solid var(--line); border-radius:var(--r); background:var(--panel); }
  .table-wrap table { min-width:660px; }
  @media (min-width:640px) {
    .t-changes td:nth-child(2), .t-changes td:nth-child(3), .t-changes td:nth-child(7),
    .t-partners td:nth-child(1), .t-partners td:nth-child(2),
    .t-firms td:nth-child(1), .t-firms td:nth-child(5),
    .t-moves td { white-space:nowrap; }
  }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:10px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { background:var(--panel2); color:var(--muted); font-weight:600; font-size:11px;
    text-transform:uppercase; letter-spacing:.6px; white-space:nowrap; }
  tbody tr:last-child td { border-bottom:none; }
  @media (hover:hover) { tbody tr:hover { background:#ffffff08; } }
  .clickable { cursor:pointer; color:var(--accent); background:none; border:0; padding:0; font:inherit; text-align:left; }
  .clickable:hover { text-decoration:underline; }

  .badge { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px;
    font-weight:600; border:1px solid var(--line); white-space:nowrap; }
  .ADDED,.b-ADDED { color:var(--good); border-color:#3ecf8e66; }
  .REMOVED,.b-REMOVED { color:var(--bad); border-color:#ff6b6b66; }
  .TITLE_CHANGED,.PROFILE_UPDATED,.SOURCE_CHANGED { color:var(--accent); border-color:#5b8cff66; }
  .OFFICE_CHANGED,.PRACTICE_CHANGED { color:var(--warn); border-color:#ffb84d66; }
  .CONTACT_UPDATED { color:var(--muted); }
  .SOURCE_DEGRADED,.COVERAGE_DROPPED { color:var(--bad); border-color:#ff6b6b66; }
  .prov { font-size:11px; color:var(--muted); border:1px solid var(--line);
    border-radius:999px; padding:1px 8px; white-space:nowrap; }
  .prov.live { color:var(--good); border-color:#3ecf8e66; }
  .st-live { color:var(--good); border-color:#3ecf8e66; }
  .st-stale { color:var(--bad); border-color:#ff6b6b66; }
  .st-sample { color:var(--warn); border-color:#ffb84d66; }
  .st-no { color:var(--muted); }

  .modal { position:fixed; inset:0; z-index:60; background:rgba(0,0,0,.6);
    display:flex; align-items:center; justify-content:center; padding:20px; }
  .modal[hidden] { display:none; }
  .modal-box { position:relative; background:var(--panel); border:1px solid var(--line);
    border-radius:14px; max-width:680px; width:100%; max-height:84vh; overflow:auto; padding:24px; }
  .modal-box .close { position:absolute; top:14px; right:14px; }
  .kv { display:grid; grid-template-columns:110px 1fr; gap:8px 14px; margin:12px 0 4px; }
  .kv .k { color:var(--muted); }
  footer { color:var(--muted); font-size:12px; padding:0 28px calc(40px + env(safe-area-inset-bottom)); }

  @media (max-width:639px) {
    body { font-size:15px; }
    .topbar { flex-direction:column; align-items:stretch; gap:10px; padding:12px 16px; }
    .brand .sub { display:none; }
    h1 { font-size:16px; }
    .summary { padding:12px 16px 0; gap:8px; flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none;
      scroll-snap-type:x proximity;
      -webkit-mask-image:linear-gradient(90deg,#000 80%,transparent);
      mask-image:linear-gradient(90deg,#000 80%,transparent); }
    .summary::-webkit-scrollbar { display:none; }
    .chip { min-width:104px; padding:8px 12px; scroll-snap-align:start; }
    .chip .n { font-size:17px; }
    .table-wrap { overflow:visible; border:0; border-radius:0; background:transparent; }
    .table-wrap table, .table-wrap tbody { display:block; min-width:0; width:100%; }
    .table-wrap thead { display:none; }
    .table-wrap tbody tr { display:block; background:var(--panel); border:1px solid var(--line);
      border-radius:var(--r); padding:6px 0; margin-bottom:8px; }
    .table-wrap tbody td { display:block; border:0; padding:5px 14px; overflow-wrap:break-word; }
    .table-wrap tbody td::before { content:attr(data-label); display:inline-block; width:86px; margin-right:8px;
      color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.6px; vertical-align:top; }
    .wrap { padding:8px 16px calc(48px + env(safe-area-inset-bottom)); }
    .toolbar { gap:8px; }
    select, .toolbar input[type=search], input[type=search] { font-size:1rem; min-height:44px; width:100%; min-width:0; }
    .btn { min-height:44px; }
    .stat { margin-left:0; }
    .modal { padding:0; align-items:flex-end; }
    .modal-box { max-width:none; width:100%; max-height:90vh; border-radius:14px 14px 0 0;
      padding:20px 18px calc(24px + env(safe-area-inset-bottom)); }
  }
  @media (pointer:coarse) { .btn, .tab { min-height:44px; } }
  @media (prefers-reduced-motion:no-preference) {
    .tab, .btn { transition:background-color .15s ease, border-color .15s ease, color .15s ease; }
    .modal-box { animation:rise .18s cubic-bezier(.2,.8,.25,1); }
    @keyframes rise { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:none; } }
  }
  @media (prefers-reduced-motion:reduce) { * { transition:none !important; animation:none !important; } }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <h1>Corporate &amp; M&amp;A Partner Intelligence</h1>
    <div class="sub">Pinpoint Partners &middot; dashboard built <span id="gen">__GENERATED__</span> &middot; data source: <span id="src">loading&hellip;</span></div>
  </div>
  <nav class="tabs" id="tabs" aria-label="Views"></nav>
</header>
<div class="summary" id="summary"></div>
<main id="view"><div class="wrap"><p class="muted">Loading&hellip;</p></div></main>
<div class="modal" id="modal" hidden><div class="modal-box" id="modalbox" role="dialog" aria-modal="true" aria-labelledby="person-title"></div></div>
<noscript><div class="wrap"><p>This dashboard needs JavaScript. The data is also available as
  <a href="partners.csv">partners.csv</a>, <a href="changes.csv">changes.csv</a> and <a href="data.json">data.json</a>.</p></div></noscript>
<footer>__FOOTER__</footer>
<script>
(function () {
  "use strict";
  var LIMIT = 40;
  var META = null;
  var firmName = {};
  var inputTimer = null;
  var requestVersion = 0;
  var detailVersion = 0;
  var lastFocus = null;
  var state = { tab: "changes", firm: "", practice: "", office: "", ctype: "",
                scope: "latest", pq: "", cq: "", psort: "name", offsetP: 0, offsetC: 0, person: null };

  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function safeUrl(u) { if (!u) return null;
    try { var p = new URL(u, location.href); return (p.protocol === "http:" || p.protocol === "https:") ? u : null; }
    catch (e) { return null; } }
  function name(slug) { return firmName[slug] || slug; }
  function stamp(v) { return String(v || "").replace("T", " ").slice(0, 19); }
  function el(id) { return document.getElementById(id); }
  function isHealth(t) { return t === "SOURCE_DEGRADED" || t === "COVERAGE_DROPPED" || t === "SOURCE_CHANGED"; }
  function tableWrap(inner) { return '<div class="table-wrap">' + inner + "</div>"; }
  function encodeQuery(o) { return Object.keys(o).filter(function (k) {
    var v = o[k]; return v !== null && v !== undefined && v !== ""; })
    .map(function (k) { return k + "=" + encodeURIComponent(o[k]); }).join("&"); }
  function get(path, params) { var url = path, q = params ? encodeQuery(params) : "";
    if (q) url += "?" + q;
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); }); }

  function parseHash() {
    var h = location.hash.replace(/^#/, ""), parts = h.split("?");
    if (["changes", "partners", "firms"].indexOf(parts[0]) >= 0) state.tab = parts[0];
    if (parts[1]) parts[1].split("&").forEach(function (kv) {
      var i = kv.indexOf("="); if (i < 0) return;
      var k = decodeURIComponent(kv.slice(0, i)), v = decodeURIComponent(kv.slice(i + 1));
      if (k === "offsetP" || k === "offsetC") state[k] = Math.max(0, parseInt(v, 10) || 0);
      else if (typeof state[k] === "string") state[k] = v;
    });
  }
  function writeHash() {
    var p = [];
    ["firm", "practice", "office", "ctype", "pq", "cq", "psort"].forEach(function (k) {
      if (state[k]) p.push(k + "=" + encodeURIComponent(state[k])); });
    if (state.scope !== "latest") p.push("scope=" + state.scope);
    if (state.offsetP > 0) p.push("offsetP=" + state.offsetP);
    if (state.offsetC > 0) p.push("offsetC=" + state.offsetC);
    history.replaceState(null, "", "#" + state.tab + (p.length ? "?" + p.join("&") : ""));
  }
  function resetOffsets() { state.offsetP = 0; state.offsetC = 0; }

  function sourceNote(m) {
    var note = m.sample_count ? "Synthetic demo" : m.counts.partners ? "Official firm sources" : "No partner records loaded";
    if (m.stale) note += " · " + m.stale + " firm(s) need review";
    return note;
  }
  function chips() {
    var c = META.counts || {};
    return [["Corporate / M&A partners", c.partners], ["Firms tracked", c.firms], ["Changes (all time)", c.changes],
            ["Added to roster", META.joined], ["Removed from roster", META.left],
            ["Firms with verified data", (META.verified || 0) + " / " + c.firms], ["Firms needing review", META.stale]]
      .map(function (it) { return '<div class="chip"><div class="n">' + esc(it[1]) + '</div><div class="l">' + esc(it[0]) + "</div></div>"; }).join("");
  }
  function optionsFor(kind) {
    var f = META.facets || {};
    if (kind === "firm") return (f.firms || []).map(function (x) { return { value: x.slug, label: x.name }; })
      .sort(function (a, b) { return a.label.localeCompare(b.label); });
    if (kind === "practice") return (f.practices || []).map(function (x) { return { value: x, label: x }; });
    if (kind === "office") return (f.offices || []).map(function (x) { return { value: x, label: x }; });
    if (kind === "ctype") return (f.types || []).map(function (x) { return { value: x, label: x }; });
    return [];
  }
  function selectHtml(kind, selected, placeholder, ariaLabel) {
    var h = '<select data-filter="' + kind + '" aria-label="' + esc(ariaLabel) + '"><option value="">' + esc(placeholder) + "</option>";
    optionsFor(kind).forEach(function (o) {
      h += '<option value="' + esc(o.value) + '"' + (selected === o.value ? " selected" : "") + ">" + esc(o.label) + "</option>";
    });
    return h + "</select>";
  }

  function changeRow(c) {
    var who = isHealth(c.change_type)
      ? '<span class="muted">&mdash;</span>'
      : action("person", c.person_key, c.full_name);
    return "<tr>"
      + '<td data-label="Type"><span class="badge ' + esc(c.change_type) + '">' + esc(c.change_type) + "</span></td>"
      + '<td data-label="Partner">' + who + "</td>"
      + '<td data-label="Firm">' + action("firm", c.firm_slug, name(c.firm_slug)) + "</td>"
      + '<td data-label="Field" class="muted">' + (esc(c.field || "") || "&mdash;") + "</td>"
      + '<td data-label="From" class="muted">' + (esc(c.old_value || "") || "&mdash;") + "</td>"
      + '<td data-label="To">' + (esc(c.new_value || "") || "&mdash;") + "</td>"
      + '<td data-label="Detected" class="muted">' + esc(stamp(c.detected_at)) + "</td></tr>";
  }
  function partnerRow(p) {
    var link = p.provenance === "sample-fixture" ? null : safeUrl(p.profile_url);
    var prof = link ? ' <a href="' + esc(link) + '" target="_blank" rel="noopener" title="Open profile" aria-label="Open profile for ' + esc(p.full_name) + '">&nearr;</a>' : "";
    var contact = [p.email, p.phone].filter(Boolean).join(" ");
    return "<tr>"
      + '<td data-label="Name">' + action("person", p.key, p.full_name) + prof
      + ' <span class="prov ' + esc(p.provenance) + '">' + esc(p.provenance) + "</span></td>"
      + '<td data-label="Firm">' + action("firm", p.firm_slug, name(p.firm_slug)) + "</td>"
      + '<td data-label="Title">' + esc(p.title) + "</td>"
      + '<td data-label="Practices">' + (esc(p.practices.join(", ")) || "&mdash;") + "</td>"
      + '<td data-label="Offices">' + (esc(p.offices.join(", ")) || "&mdash;") + "</td>"
      + '<td data-label="Contact" class="muted">' + (esc(contact) || "&mdash;") + "</td></tr>";
  }
  function action(kind, id, label) {
    return '<button type="button" class="clickable" data-' + kind + '="' + esc(id) + '">' + esc(label) + '</button>';
  }
  function firmRow(f) {
    var verified = ["live", "reviewed", "verified empty"].includes(f.status);
    var hasGap = f.status === "stale" || f.status === "coverage gap";
    var cls = verified ? "st-live" : hasGap ? "st-stale" : f.status === "sample" ? "st-sample" : "st-no";
    var source = safeUrl(f.source_url);
    var partnerCount = (f.partners || f.last_verified) ? esc(f.partners) : "Unknown";
    return "<tr>"
      + '<td data-label="Firm">' + action("firm", f.slug, f.name) + "</td>"
      + '<td data-label="Status"><span class="badge ' + cls + '">' + esc(f.status) + "</span></td>"
      + '<td data-label="Active partners">' + partnerCount + '</td>'
      + '<td data-label="Verified" class="muted">' + (f.last_verified ? esc(stamp(f.last_verified)) + " UTC" : "&mdash;") + "</td>"
      + '<td data-label="Evidence">' + (source ? '<a href="' + esc(source) + '" target="_blank" rel="noopener noreferrer">Official source</a> ' : "")
      + (hasGap ? esc(f.reason) : "") + "</td></tr>";
  }

  function pager(total, offset, key) {
    var from = total ? offset + 1 : 0, to = Math.min(offset + LIMIT, total);
    var disPrev = offset <= 0, lastOffset = Math.max(0, (Math.ceil(total / LIMIT) - 1) * LIMIT);
    var disNext = offset + LIMIT >= total;
    return '<div class="pager">'
      + '<button class="btn" data-nav="' + key + ':0"' + (disPrev ? " disabled" : "") + ">&laquo; First</button>"
      + '<button class="btn" data-nav="' + key + ":" + Math.max(0, offset - LIMIT) + '"' + (disPrev ? " disabled" : "") + ">Prev</button>"
      + "<span>" + from + "&ndash;" + to + " of " + total + "</span>"
      + '<button class="btn" data-nav="' + key + ":" + (offset + LIMIT) + '"' + (disNext ? " disabled" : "") + ">Next</button>"
      + '<button class="btn" data-nav="' + key + ":" + lastOffset + '"' + (disNext ? " disabled" : "") + ">Last &raquo;</button>"
      + "</div>";
  }
  function emptyState(title, hint) {
    return '<p class="muted" style="padding:20px 12px">' + esc(title) + ' <span class="small">' + esc(hint) + "</span></p>";
  }

  function changesChrome() {
    return '<section class="wrap"><div class="toolbar">'
      + selectHtml("firm", state.firm, "All firms", "Filter by firm")
      + selectHtml("ctype", state.ctype, "All types", "Filter by change type")
      + '<label class="seg"><input type="radio" name="scope" value="latest"' + (state.scope === "latest" ? " checked" : "") + "> This refresh</label>"
      + '<label class="seg"><input type="radio" name="scope" value="all"' + (state.scope !== "latest" ? " checked" : "") + "> All time</label>"
      + '<input type="search" data-filter="cq" aria-label="Search changes" placeholder="Search partner or value&hellip;" value="' + esc(state.cq) + '">'
      + '<button class="btn" data-act="clear">Clear</button><span class="stat" id="count" aria-live="polite"></span></div>'
      + '<div id="body"><p class="muted">Loading changes&hellip;</p></div></section>';
  }
  function partnersChrome() {
    return '<section class="wrap"><div class="toolbar">'
      + selectHtml("firm", state.firm, "All firms", "Filter by firm")
      + selectHtml("practice", state.practice, "All practices", "Filter by practice")
      + selectHtml("office", state.office, "All offices", "Filter by office")
      + '<input type="search" data-filter="pq" aria-label="Search partners" placeholder="Search name, title&hellip;" value="' + esc(state.pq) + '">'
      + '<select data-filter="psort" aria-label="Sort order"><option value="name"' + (state.psort === "name" ? " selected" : "") + '>Sort: name</option><option value="firm"' + (state.psort === "firm" ? " selected" : "") + ">Sort: firm</option></select>"
      + '<button class="btn" data-act="clear">Clear</button><span class="stat" id="count" aria-live="polite"></span></div>'
      + '<div id="body"><p class="muted">Loading partners&hellip;</p></div></section>';
  }
  function firmsChrome() {
    return '<section class="wrap"><h2>Firms <span class="muted">&mdash; coverage &amp; freshness</span></h2>'
      + '<div id="body"><p class="muted">Loading firms&hellip;</p></div></section>';
  }
  function modalHtml(p, history, firm) {
    var link = p.provenance === "sample-fixture" ? null : safeUrl(p.profile_url);
    var verified = firm.last_verified ? stamp(firm.last_verified) + " UTC" : "—";
    var h = '<button class="btn close" data-act="close">Close</button><h2 id="person-title">' + esc(p.full_name) + "</h2>"
      + '<div class="kv">'
      + '<div class="k">Firm</div><div>' + esc(name(p.firm_slug)) + "</div>"
      + '<div class="k">Title</div><div>' + (esc(p.title) || "&mdash;") + "</div>"
      + '<div class="k">Practices</div><div>' + (esc(p.practices.join(", ")) || "&mdash;") + "</div>"
      + '<div class="k">Offices</div><div>' + (esc(p.offices.join(", ")) || "&mdash;") + "</div>"
      + '<div class="k">Contact</div><div>' + (esc([p.email, p.phone].filter(Boolean).join(" ")) || "&mdash;") + "</div>"
      + '<div class="k">Source</div><div><span class="prov ' + esc(p.provenance) + '">' + esc(p.provenance) + "</span></div>"
      + '<div class="k">Verified</div><div>' + esc(verified) + (firm.status === "stale" ? " (refresh pending)" : "") + "</div>"
      + '<div class="k">Profile</div><div>' + (link ? '<a href="' + esc(link) + '" target="_blank" rel="noopener">' + esc(link) + "</a>" : "&mdash;") + "</div>"
      + '</div><h3>Change history <span class="muted">(recent window)</span></h3>';
    if (!history || !history.length) return h + '<p class="muted small">No recorded changes.</p>';
    h += '<div class="table-wrap"><table class="t-history"><thead><tr><th>Type</th><th>Field</th><th>From</th><th>To</th><th>Detected</th></tr></thead><tbody>';
    history.forEach(function (c) {
      h += '<tr><td data-label="Type"><span class="badge ' + esc(c.change_type) + '">' + esc(c.change_type) + '</span></td>'
        + '<td data-label="Field" class="muted">' + (esc(c.field || "") || "&mdash;") + '</td>'
        + '<td data-label="From" class="muted">' + (esc(c.old_value || "") || "&mdash;") + '</td>'
        + '<td data-label="To">' + (esc(c.new_value || "") || "&mdash;") + '</td>'
        + '<td data-label="Detected" class="muted">' + esc(stamp(c.detected_at)) + "</td></tr>";
    });
    return h + "</tbody></table></div>";
  }

  function showError(err) {
    var b = el("body") || el("view");
    b.innerHTML = '<p class="muted">Could not load data (' + esc(err.message) + "). Is the server running? "
      + "Start it with <code>python3 -m pinpoint serve --build</code>.</p>";
  }

  function renderTabs() {
    var c = META.counts || {};
    var tabs = [["changes", "Changes", c.changes], ["partners", "Partners", c.partners], ["firms", "Firms", c.firms]];
    el("tabs").innerHTML = tabs.map(function (t) {
      return '<button class="tab' + (state.tab === t[0] ? " active" : "") + '" data-tab="' + t[0] + '" aria-current="' + (state.tab === t[0] ? "true" : "false") + '">'
        + t[1] + ' <span class="count">' + esc(t[2] == null ? "" : t[2]) + "</span></button>";
    }).join("");
  }
  function renderHeader() {
    if (!META) return;
    renderTabs();
    el("summary").innerHTML = chips();
  }
  function render() {
    if (!META) return;  // nothing to render until the API bootstrap succeeds
    renderHeader();
    var v = el("view");
    v.innerHTML = state.tab === "partners" ? partnersChrome() : state.tab === "firms" ? firmsChrome() : changesChrome();
    load();
  }

  function load() {
    if (!META) return;
    if (state.tab === "firms") return loadFirms();
    if (state.tab === "partners") return loadPartners();
    return loadChanges();
  }
  function setBody(html) { var b = el("body"); if (b) b.innerHTML = html; }
  function setCount(n, what) { var c = el("count"); if (c) c.textContent = n + " " + what; }
  function loading(what) { setBody('<p class="muted">Loading ' + esc(what) + "&hellip;</p>"); }

  function loadChanges() {
    var version = ++requestVersion;
    loading("changes");
    get("/api/changes", { firm: state.firm, type: state.ctype, snapshot: state.scope === "latest" ? "latest" : "",
                          q: state.cq, limit: LIMIT, offset: state.offsetC })
      .then(function (res) {
        if (version !== requestVersion) return;
        setBody(res.items.length
          ? tableWrap('<table class="t-changes"><thead><tr><th>Type</th><th>Partner</th><th>Firm</th><th>Field</th><th>From</th><th>To</th><th>Detected</th></tr></thead><tbody>'
            + res.items.map(changeRow).join("") + "</tbody></table>") + pager(res.total, state.offsetC, "changes")
          : emptyState("No changes match this filter.", "Try clearing the search or widening the firm."));
        setCount(res.total, "change(s)");
      }).catch(function (err) { if (version === requestVersion) showError(err); });
  }
  function loadPartners() {
    var version = ++requestVersion;
    loading("partners");
    get("/api/partners", { firm: state.firm, practice: state.practice, office: state.office,
                           q: state.pq, sort: state.psort, limit: LIMIT, offset: state.offsetP })
      .then(function (res) {
        if (version !== requestVersion) return;
        setBody(res.items.length
          ? tableWrap('<table class="t-partners"><thead><tr><th>Name</th><th>Firm</th><th>Title</th><th>Practices</th><th>Offices</th><th>Contact</th></tr></thead><tbody>'
            + res.items.map(partnerRow).join("") + "</tbody></table>") + pager(res.total, state.offsetP, "partners")
          : emptyState("No partners match this filter.", "Try clearing the search or widening the firm or office."));
        setCount(res.total, "partner(s)");
      }).catch(function (err) { if (version === requestVersion) showError(err); });
  }
  function loadFirms() {
    var version = ++requestVersion;
    loading("firms");
    get("/api/firms").then(function (rows) {
      if (version !== requestVersion) return;
      setBody(rows.length
        ? tableWrap('<table class="t-firms"><thead><tr><th>Firm</th><th>Status</th><th>Partners</th><th>Last verified</th><th>Evidence</th></tr></thead><tbody>'
          + rows.map(firmRow).join("") + "</tbody></table>")
        : emptyState("No firms configured.", "Run python3 -m pinpoint init to register the firms."));
    }).catch(function (err) { if (version === requestVersion) showError(err); });
  }

  function setInert(on) {
    ["summary", "view"].forEach(function (id) { var n = el(id); if (n) n.inert = on; });
    var hd = document.querySelector("header"); if (hd) hd.inert = on;
  }
  function openPerson(key) {
    var version = ++detailVersion;
    state.person = key;
    lastFocus = document.activeElement;
    get("/api/person", { key: key }).then(function (res) {
      if (version !== detailVersion) return;
      el("modalbox").innerHTML = modalHtml(res.person, res.history, res.firm || {});
      el("modal").hidden = false;
      setInert(true);
      var c = el("modalbox").querySelector("[data-act=close]"); if (c) c.focus();
    }).catch(function (e) { if (version === detailVersion) { state.person = null; showError(e); } });
  }
  function closePerson() {
    ++detailVersion;
    state.person = null;
    el("modal").hidden = true;
    el("modalbox").innerHTML = "";
    setInert(false);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
  }

  function boot() {
    get("/api/meta").then(function (meta) {
      META = meta;
      if (!location.hash && !meta.sample_count) state.tab = meta.counts.partners ? "partners" : "firms";
      (meta.facets.firms || []).forEach(function (f) { firmName[f.slug] = f.name; });
      el("src").textContent = sourceNote(meta);
      render();
    }).catch(function (e) { el("src").textContent = "unavailable"; showError(e); });
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-tab],[data-firm],[data-person],[data-nav],[data-act]");
    if (!t) return;
    if (t.hasAttribute("data-tab")) { state.tab = t.getAttribute("data-tab"); resetOffsets(); writeHash(); return render(); }
    if (t.hasAttribute("data-person")) return openPerson(t.getAttribute("data-person"));
    if (t.hasAttribute("data-firm")) { state.tab = "partners"; state.firm = t.getAttribute("data-firm"); state.practice = ""; state.office = ""; resetOffsets(); writeHash(); return render(); }
    if (t.hasAttribute("data-nav")) { var pv = t.getAttribute("data-nav").split(":"); state[pv[0] === "partners" ? "offsetP" : "offsetC"] = Math.max(0, parseInt(pv[1], 10) || 0); writeHash(); return load(); }
    var act = t.getAttribute("data-act");
    if (act === "clear") { state.firm = ""; state.practice = ""; state.office = ""; state.ctype = ""; state.pq = ""; state.cq = ""; resetOffsets(); writeHash(); return render(); }
    if (act === "close") return closePerson();
  });
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.name === "scope" && t.checked) { state.scope = t.value; state.offsetC = 0; writeHash(); return load(); }
    if (t.matches && t.matches("[data-filter]")) { state[t.getAttribute("data-filter")] = t.value; resetOffsets(); writeHash(); return load(); }
  });
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t.matches && t.matches("input[type=search][data-filter]")) {
      var k = t.getAttribute("data-filter"); state[k] = t.value;
      state[k === "pq" ? "offsetP" : "offsetC"] = 0;
      writeHash();
      if (inputTimer) clearTimeout(inputTimer);
      inputTimer = setTimeout(load, 200);
    }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && state.person) closePerson(); });
  el("modal").addEventListener("click", function (e) { if (e.target === el("modal")) closePerson(); });
  el("modal").addEventListener("keydown", function (e) {
    if (e.key !== "Tab") return;
    var f = el("modalbox").querySelectorAll("a[href],button:not([disabled]),input,select,textarea,[tabindex]:not([tabindex='-1'])");
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  window.addEventListener("hashchange", function () { parseHash(); render(); });

  parseHash();
  boot();
})();
</script>
</body>
</html>
"""


def _esc(value: object) -> str:
    """HTML-escape any value for safe interpolation."""
    return html.escape("" if value is None else str(value), quote=True)


def build_report(
    store: Store,
    firms: list[Firm],
    out_dir: Path | str,
    generated_at: str,
) -> dict[str, Path]:
    """Write the dashboard shell plus ``partners.csv``, ``changes.csv``, ``data.json``.

    The shell needs the JSON API (``pinpoint serve``) to display data; the CSVs
    and ``data.json`` are standalone exports. Returns artefact name -> path.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    firm_names = {f.slug: f.name for f in firms}
    partners = store.list_persons()
    changes = store.recent_changes(limit=500)

    latest = store.latest_snapshot_id()
    flags = store.health_flags_for_snapshot(latest)
    firm_rows = store.firm_health()
    for row in firm_rows:
        row["status"] = firm_status(row, flags.get(row["slug"]))

    # Scrub profile URLs at the source so no unsafe scheme is ever exported.
    partner_rows = []
    for person in partners:
        data = person.to_dict()
        data["profile_url"] = safe_url(data["profile_url"])
        partner_rows.append(data)

    html_path = out / "index.html"
    html_path.write_text(
        _SHELL.replace("__GENERATED__", _esc(generated_at)).replace("__FOOTER__", _footer_note()),
        encoding="utf-8",
    )
    _write_partners_csv(out / "partners.csv", partners, firm_names)
    _write_changes_csv(out / "changes.csv", changes, firm_names)

    data_json = out / "data.json"
    data_json.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "latest_snapshot": latest,
                "partners": partner_rows,
                "changes": changes,
                "firms": firm_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "index": html_path,
        "partners_csv": out / "partners.csv",
        "changes_csv": out / "changes.csv",
        "data_json": data_json,
    }


def _footer_note() -> str:
    """Static provenance/conduct note shown in the footer."""
    return (
        "Check Firms for coverage, verification dates, and official sources."
    )


def _write_partners_csv(path: Path, partners, firm_names: dict[str, str]) -> None:
    """Write the partner list as CSV."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["full_name", "firm", "title", "practices", "offices", "email", "phone", "profile_url", "provenance"]
        )
        for p in partners:
            writer.writerow(map(_csv_cell,
                [
                    p.full_name,
                    firm_names.get(p.firm_slug, p.firm_slug),
                    p.title,
                    "; ".join(p.practices),
                    "; ".join(p.offices),
                    p.email or "",
                    p.phone or "",
                    safe_url(p.profile_url) or "",
                    p.provenance,
                ]
            ))


def _write_changes_csv(path: Path, changes: list[dict], firm_names: dict[str, str]) -> None:
    """Write the change log as CSV."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["detected_at", "change_type", "full_name", "firm", "field", "old_value", "new_value"]
        )
        for c in changes:
            writer.writerow(map(_csv_cell,
                [
                    c.get("detected_at", ""),
                    c.get("change_type", ""),
                    c.get("full_name", ""),
                    firm_names.get(c.get("firm_slug", ""), c.get("firm_slug", "")),
                    c.get("field", ""),
                    c.get("old_value", ""),
                    c.get("new_value", ""),
                ]
            ))


def _csv_cell(value: object) -> str:
    """Keep spreadsheet formula prefixes inert in contact and change exports."""
    text = str(value) if value is not None else ""
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text
