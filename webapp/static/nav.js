/* Shared left activity rail — consistent navigation + active-pack context on every
   screen. Injected on all pages via <script src="/static/nav.js">. */
(function () {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  const groups = [
    { h: "Workspace", items: [{ href: "/packs", icon: "📦", label: "Packs" }] },
    { h: "Test", items: [{ href: "/", icon: "💬", label: "Bake-off" }] },
    { h: "Optimize", items: [
        { href: "/tune", icon: "🔧", label: "Tune" },
        { href: "/advisor", icon: "🧭", label: "Advisor" }] },
    { h: "Verify", items: [
        { href: "/eval", icon: "🧪", label: "Eval" },
        { href: "/validate", icon: "🔬", label: "Validate" }] },
  ];
  const css = `
   #tbrail{position:fixed;left:0;top:0;bottom:0;width:80px;background:#090c12;border-right:1px solid #222a3b;
     display:flex;flex-direction:column;align-items:center;padding:12px 0;z-index:60;overflow-y:auto}
   #tbrail .logo{width:14px;height:14px;border-radius:50%;background:linear-gradient(135deg,#7aa2ff,#a78bfa);
     box-shadow:0 0 12px #7aa2ff;margin-bottom:10px;flex:none}
   #tbrail .pack{display:block;text-decoration:none;text-align:center;font-size:9px;color:#8a93a8;line-height:1.15;
     max-width:72px;margin-bottom:6px;padding:6px 4px;border:1px solid #222a3b;border-radius:9px;flex:none}
   #tbrail .pack:hover{border-color:#7aa2ff} #tbrail .pack b{color:#e8ebf2;display:block;font-size:10px;margin-top:2px}
   #tbrail .grp{font-size:8px;letter-spacing:.07em;text-transform:uppercase;color:#465066;margin:10px 0 2px;flex:none}
   #tbrail a.nav{display:flex;flex-direction:column;align-items:center;gap:3px;width:66px;padding:8px 0;margin:1px 0;
     border-radius:11px;text-decoration:none;color:#8a93a8;font-size:10px;flex:none}
   #tbrail a.nav .ic{font-size:19px}
   #tbrail a.nav:hover{background:#141925;color:#e8ebf2}
   #tbrail a.nav.on{background:#141925;color:#e8ebf2;box-shadow:inset 3px 0 0 #7aa2ff}
   body{padding-left:80px}
  `;
  const st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);

  const rail = document.createElement("div"); rail.id = "tbrail";
  let html = `<div class="logo" title="TiefBench"></div>
    <a class="pack" href="/packs" title="Active pack — click to manage">pack<b id="tbpack">…</b></a>`;
  for (const g of groups) {
    html += `<div class="grp">${g.h}</div>`;
    for (const it of g.items) {
      const on = (it.href === "/" ? path === "/" : path.startsWith(it.href)) ? "on" : "";
      html += `<a class="nav ${on}" href="${it.href}" title="${it.label}"><span class="ic">${it.icon}</span>${it.label}</a>`;
    }
  }
  rail.innerHTML = html;
  document.body.appendChild(rail);

  fetch("/api/packs").then(r => r.json()).then(d => {
    const el = document.getElementById("tbpack");
    if (el) el.textContent = (d.active_name || "—").slice(0, 18);
  }).catch(() => {});
})();
