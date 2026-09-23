/* =========================================================================
   IVZ Globe — globo terráqueo 3D interactivo, compartido por
   IVZ Sustainability Hub e IVZ Carbon.
   Canvas 2D con proyección ortográfica propia: no usa WebGL, teselas ni
   servicios de mapas, así que funciona igual en cualquier navegador y sin
   conexión. Los contornos vienen de world.js (Natural Earth, dominio público).

   const g = IVZGlobe.create(div, {tooltip, onClick, onPick, lat, lon, zoom});
   g.setData({points:[…], arcs:[…], heat:[…]});   g.select(id);   g.fit(latLons);
   g.flyTo(lat, lon, zoom);   g.setPickMode(true);   g.getView();   g.destroy();
   ========================================================================= */
(function () {
  'use strict';
  const D2R = Math.PI / 180, R2D = 180 / Math.PI, TAU = Math.PI * 2;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const vec = (lat, lon) => { const a = lat * D2R, b = lon * D2R, c = Math.cos(a); return [c * Math.cos(b), c * Math.sin(b), Math.sin(a)]; };
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const angle = (a, b) => Math.acos(clamp(dot(a, b), -1, 1));
  function slerp(a, b, t) {
    const w = angle(a, b);
    if (w < 1e-6) return a.slice();
    const s = Math.sin(w), k1 = Math.sin((1 - t) * w) / s, k2 = Math.sin(t * w) / s;
    return [a[0] * k1 + b[0] * k2, a[1] * k1 + b[1] * k2, a[2] * k1 + b[2] * k2];
  }

  /* ---------------------------- datos del mundo ------------------------- */
  let WORLD = null, SD = new Float32Array(4096), SU = new Float32Array(4096), SW = new Float32Array(4096);
  function world() {
    if (WORLD || !window.IVZ_WORLD) return WORLD;
    const W = window.IVZ_WORLD;
    const arcs = W.a.map(f => {
      const n = f.length / 2, out = new Float32Array(n * 3); let x = 0, y = 0;
      for (let i = 0; i < n; i++) { x += f[2 * i]; y += f[2 * i + 1]; const v = vec(y / 100, x / 100); out[3 * i] = v[0]; out[3 * i + 1] = v[1]; out[3 * i + 2] = v[2]; }
      return out;
    });
    const rings = W.r.map(r => {
      const pts = [];
      r.forEach((ai, k) => {
        const rev = ai < 0, a = arcs[rev ? ~ai : ai], n = a.length / 3;
        for (let j = 0; j < n; j++) { if (k > 0 && j === 0) continue; const i = rev ? n - 1 - j : j; pts.push(a[3 * i], a[3 * i + 1], a[3 * i + 2]); }
      });
      const p = new Float32Array(pts); let polar = false;
      for (let i = 2; i < p.length; i += 3) if (Math.abs(p[i]) > 0.985) { polar = true; break; }
      return { p, polar };
    });
    const grat = [];
    for (let lon = -180; lon < 180; lon += 30) { const l = []; for (let lat = -80; lat <= 80; lat += 2) l.push(...vec(lat, lon)); grat.push(new Float32Array(l)); }
    for (let lat = -60; lat <= 60; lat += 30) { const l = []; for (let lon = -180; lon <= 180; lon += 2) l.push(...vec(lat, lon)); grat.push(new Float32Array(l)); }
    WORLD = { arcs, rings, grat };
    return WORLD;
  }

  /* ------------------------------- estilos ------------------------------ */
  function injectCSS() {
    if (document.getElementById('ivzg-css')) return;
    const s = document.createElement('style'); s.id = 'ivzg-css';
    s.textContent = `
.ivzg{overflow:hidden;background:radial-gradient(ellipse at 45% 42%,#12331f 0%,#0a1f14 52%,#06110b 100%);touch-action:none;user-select:none;-webkit-user-select:none}
.ivzg>canvas{position:absolute;inset:0;width:100%;height:100%;cursor:grab;display:block}
.ivzg.drag>canvas{cursor:grabbing}.ivzg.hot>canvas{cursor:pointer}.ivzg.pick>canvas{cursor:crosshair}
.ivzg-ctl{position:absolute;right:12px;bottom:12px;display:flex;flex-direction:column;gap:6px;z-index:3}
.ivzg-ctl button{width:32px;height:32px;border-radius:8px;border:1px solid rgba(190,230,205,.22);background:rgba(8,24,16,.74);color:#DCEFE3;font:600 16px/1 Inter,system-ui,sans-serif;cursor:pointer;display:grid;place-items:center;padding:0}
.ivzg-ctl button:hover{background:rgba(34,78,54,.92)}.ivzg-ctl button.on{border-color:rgba(143,211,163,.7);color:#8FD3A3}
.ivzg-ctl svg{width:16px;height:16px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.ivzg-tip{position:absolute;z-index:4;pointer-events:none;max-width:290px;background:rgba(7,20,13,.95);color:#E8F4EC;border:1px solid rgba(160,220,185,.25);border-radius:8px;padding:8px 10px;font:12px/1.45 Inter,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.35);opacity:0;transition:opacity .12s}
.ivzg-tip.on{opacity:1}.ivzg-tip b{color:#fff}.ivzg-tip .d{color:#9FBBA9}
.ivzg-hint{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);background:rgba(7,20,13,.88);color:#DCEFE3;font:12px Inter,system-ui,sans-serif;padding:6px 12px;border-radius:999px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:3;white-space:nowrap}
.ivzg-hint.on{opacity:1}
.ivzg-empty{position:absolute;inset:0;display:grid;place-items:center;color:#C9DDD0;font:13px Inter,system-ui,sans-serif;text-align:center;padding:24px;z-index:2}`;
    document.head.appendChild(s);
  }
  const ICON = {
    plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
    minus: '<svg viewBox="0 0 24 24"><path d="M5 12h14"/></svg>',
    home: '<svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>',
    spin: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>'
  };

  /* colores: rgba a partir de #hex y alfa */
  const rgbCache = {};
  function rgba(hex, a) {
    let c = rgbCache[hex];
    if (!c) { const h = hex.replace('#', ''); const n = parseInt(h.length === 3 ? h.split('').map(x => x + x).join('') : h, 16); c = rgbCache[hex] = [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
    return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + clamp(a, 0, 1).toFixed(3) + ')';
  }
  /* escala de emisión: verde (bajo) → ámbar → rojo (alto), t en 0..1 */
  const RAMP = [[0, [61, 220, 132]], [0.5, [242, 201, 76]], [0.78, [255, 122, 58]], [1, [255, 59, 48]]];
  function ramp(t) {
    t = clamp(t, 0, 1);
    for (let i = 1; i < RAMP.length; i++) if (t <= RAMP[i][0]) {
      const [t0, a] = RAMP[i - 1], [t1, b] = RAMP[i], k = (t - t0) / (t1 - t0);
      return '#' + a.map((v, j) => Math.round(v + (b[j] - v) * k).toString(16).padStart(2, '0')).join('');
    }
    return '#ff3b30';
  }

  /* paleta del mapa de calor: 256 niveles de intensidad → rgba */
  let HEAT = null;
  function HEAT_LUT() {
    if (HEAT) return HEAT;
    HEAT = new Uint8ClampedArray(256 * 4);
    for (let i = 0; i < 256; i++) {
      const t = i / 255, col = ramp(clamp((t - 0.04) / 0.7, 0, 1)), n = parseInt(col.slice(1), 16);
      HEAT[i * 4] = (n >> 16) & 255; HEAT[i * 4 + 1] = (n >> 8) & 255; HEAT[i * 4 + 2] = n & 255;
      HEAT[i * 4 + 3] = Math.round(255 * clamp(t * 2.2, 0, 0.78));
    }
    return HEAT;
  }

  /* =============================== globo ================================ */
  class Globe {
    constructor(host, o) {
      injectCSS();
      this.host = host; this.o = o || {};
      this.lat = this.o.lat != null ? this.o.lat : -15; this.lon = this.o.lon != null ? this.o.lon : -55;
      this.zoom = this.o.zoom || 1; this.home = { lat: this.lat, lon: this.lon, zoom: this.zoom };
      this.spin = !!this.o.autoRotate; this.lastUser = 0;
      this.pts = []; this.arcs = []; this.heat = [];
      this.sel = null; this.hot = null; this.pickMode = false; this.dirty = true; this.alive = true; this.visible = true;
      this.vel = [0, 0]; this.pointers = new Map();
      host.classList.add('ivzg'); host.innerHTML = '';
      if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
      this.cv = document.createElement('canvas'); this.cv.setAttribute('role', 'img'); this.cv.setAttribute('aria-label', this.o.label || 'Globo terráqueo interactivo');
      this.ctx = this.cv.getContext('2d');
      this.baseCv = document.createElement('canvas'); this.bctx = this.baseCv.getContext('2d');
      this.tip = document.createElement('div'); this.tip.className = 'ivzg-tip';
      this.hint = document.createElement('div'); this.hint.className = 'ivzg-hint'; this.hint.textContent = 'Usá Ctrl + rueda (o los botones) para acercar';
      const ctl = document.createElement('div'); ctl.className = 'ivzg-ctl';
      const btn = (ic, title, fn) => { const b = document.createElement('button'); b.type = 'button'; b.innerHTML = ICON[ic]; b.title = title; b.setAttribute('aria-label', title); b.onclick = e => { e.stopPropagation(); fn(b); }; ctl.appendChild(b); return b; };
      btn('plus', 'Acercar', () => this.zoomBy(1.45));
      btn('minus', 'Alejar', () => this.zoomBy(1 / 1.45));
      btn('home', 'Vista inicial', () => this.flyTo(this.home.lat, this.home.lon, this.home.zoom));
      this.spinBtn = btn('spin', 'Rotación automática', b => { this.spin = !this.spin; this.lastUser = 0; b.classList.toggle('on', this.spin); });
      this.spinBtn.classList.toggle('on', this.spin);
      host.append(this.cv, ctl, this.tip, this.hint);
      this._bind();
      this.ro = new ResizeObserver(() => this._resize()); this.ro.observe(host);
      if ('IntersectionObserver' in window) { this.io = new IntersectionObserver(es => { this.visible = es[0].isIntersecting; }); this.io.observe(host); }
      this._resize();
      this.prevT = performance.now();
      const loop = t => { if (!this.alive) return; this._frame(t); this.raf = requestAnimationFrame(loop); };
      this.raf = requestAnimationFrame(loop);
    }

    /* ---------------------------- API pública --------------------------- */
    setData(d) {
      d = d || {};
      this.pts = (d.points || []).filter(p => isFinite(p.lat) && isFinite(p.lon)).map(p => Object.assign({ kind: 'point', v: vec(p.lat, p.lon) }, p));
      this.heat = (d.heat || []).filter(p => isFinite(p.lat) && isFinite(p.lon)).map(p => Object.assign({ kind: 'heat', v: vec(p.lat, p.lon), ph: Math.random() * TAU }, p));
      this.arcs = (d.arcs || []).map(a => this._arc(a)).filter(Boolean);
      if (this.sel && !this._find(this.sel)) this.sel = null;
      this.hot = null; this._tip(null);
      this.dirty = true; this.heatDirty = true;
    }
    select(id, fly) {
      this.sel = id || null; this.heatDirty = true;
      const it = id ? this._find(id) : null;
      if (it && fly !== false) {
        if (it.kind === 'arc') this.fit(it.ll, { maxZoom: 4 });
        else this.flyTo(it.lat, it.lon, Math.max(this.zoom, 1.6));
      }
    }
    getView() { return { lat: this.lat, lon: this.lon, zoom: this.zoom }; }
    setView(v, asHome) { if (!v) return; this.lat = v.lat; this.lon = v.lon; this.zoom = v.zoom || 1; if (asHome) this.home = this.getView(); this.dirty = true; }
    setHome(v) { this.home = Object.assign({}, v); }
    zoomBy(k) { this.flyTo(this.lat, this.lon, clamp(this.zoom * k, 0.8, 9), 350); }
    flyTo(lat, lon, zoom, ms) {
      const from = this.getView(); let dl = ((lon - from.lon + 540) % 360) - 180;
      this.anim = { t0: performance.now(), ms: ms || 900, from, to: { lat: clamp(lat, -80, 80), lon: from.lon + dl, zoom: clamp(zoom || from.zoom, 0.8, 9) } };
      this.lastUser = performance.now();
    }
    /* centra y ajusta el zoom para que entren todas las coordenadas */
    fit(list, opt) {
      const vs = (list || []).filter(p => p && isFinite(p[0]) && isFinite(p[1])).map(p => vec(p[0], p[1]));
      if (!vs.length) return;
      const c = [0, 0, 0]; vs.forEach(v => { c[0] += v[0]; c[1] += v[1]; c[2] += v[2]; });
      const n = Math.hypot(c[0], c[1], c[2]);
      let lat, lon, zoom = 1;
      if (n < 1e-6) { lat = this.lat; lon = this.lon; }
      else {
        c[0] /= n; c[1] /= n; c[2] /= n;
        lat = Math.asin(c[2]) * R2D; lon = Math.atan2(c[1], c[0]) * R2D;
        const mx = Math.max(...vs.map(v => angle(v, c)));
        zoom = mx >= Math.PI / 2 ? 1 : clamp(0.92 / Math.max(Math.sin(mx), 0.04), 1, (opt && opt.maxZoom) || 5);
        /* mirar un poco desde el sur: los pines quedan en la mitad superior y se ven de costado */
        if (opt && opt.tilt) lat = clamp(lat - opt.tilt, -75, 75);
      }
      if (opt && opt.home) this.home = { lat, lon, zoom };
      if (opt && opt.instant) { this.lat = lat; this.lon = lon; this.zoom = zoom; this.dirty = true; } else this.flyTo(lat, lon, zoom);
    }
    setPickMode(on) { this.pickMode = !!on; this.host.classList.toggle('pick', this.pickMode); }
    setEmpty(text) {
      if (!this.emptyEl) { this.emptyEl = document.createElement('div'); this.emptyEl.className = 'ivzg-empty'; this.host.appendChild(this.emptyEl); }
      this.emptyEl.style.display = text ? '' : 'none'; this.emptyEl.textContent = text || '';
    }
    destroy() {
      this.alive = false; cancelAnimationFrame(this.raf);
      try { this.ro.disconnect(); } catch (e) { }
      try { this.io && this.io.disconnect(); } catch (e) { }
      this.host.innerHTML = ''; this.host.classList.remove('ivzg', 'drag', 'hot', 'pick');
    }

    /* --------------------------- preparación ---------------------------- */
    _arc(a) {
      const path = a.path || [a.from, a.to];
      const ll = path.filter(p => p && isFinite(p[0]) && isFinite(p[1])), vs = ll.map(p => vec(p[0], p[1]));
      if (vs.length < 2) return null;
      const segs = []; let tot = 0;
      for (let i = 1; i < vs.length; i++) { const w = angle(vs[i - 1], vs[i]); segs.push(w); tot += w; }
      const alt = a.alt != null ? a.alt : clamp(0.03 + tot * 0.32, 0.03, 0.36);
      const pts = []; let acc = 0;
      for (let i = 1; i < vs.length; i++) {
        const n = Math.max(2, Math.ceil(segs[i - 1] * R2D / 1.2));
        for (let k = (i === 1 ? 0 : 1); k <= n; k++) {
          const t = k / n, p = slerp(vs[i - 1], vs[i], t), s = tot ? (acc + segs[i - 1] * t) / tot : 0, h = 1 + alt * Math.sin(Math.PI * s);
          pts.push(p[0] * h, p[1] * h, p[2] * h);
        }
        acc += segs[i - 1];
      }
      return Object.assign({ kind: 'arc', p: new Float32Array(pts), n: pts.length / 3, ll, ang: tot, ph: Math.random(), dur: 1500 + tot * 2600 }, a);
    }
    _find(id) { return this.pts.find(x => x.id === id) || this.arcs.find(x => x.id === id) || this.heat.find(x => x.id === id) || null; }

    _resize() {
      const r = this.host.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
      this.w = Math.max(10, r.width); this.h = Math.max(10, r.height); this.dpr = dpr;
      [this.cv, this.baseCv].forEach(c => { c.width = Math.round(this.w * dpr); c.height = Math.round(this.h * dpr); });
      this.dirty = true;
    }
    _geom() {
      const l = this.lon * D2R, p = this.lat * D2R, cl = Math.cos(l), sl = Math.sin(l), cp = Math.cos(p), sp = Math.sin(p);
      this.M = [cp * cl, cp * sl, sp, -sl, cl, 0, -sp * cl, -sp * sl, cp];
      this.cx = this.w / 2; this.cy = this.h / 2; this.R = Math.min(this.w, this.h) * 0.42 * this.zoom;
    }

    /* ------------------------------ render ------------------------------ */
    _frame(now) {
      const dt = Math.min(64, now - this.prevT); this.prevT = now;
      if (this.anim) {
        const a = this.anim, k = clamp((now - a.t0) / a.ms, 0, 1), e = k < .5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
        this.lat = a.from.lat + (a.to.lat - a.from.lat) * e; this.lon = a.from.lon + (a.to.lon - a.from.lon) * e;
        this.zoom = a.from.zoom + (a.to.zoom - a.from.zoom) * e; this.dirty = true;
        if (k >= 1) this.anim = null;
      } else if (!this.pointers.size && (Math.abs(this.vel[0]) > 0.002 || Math.abs(this.vel[1]) > 0.002)) {
        this.lon -= this.vel[0] * dt; this.lat = clamp(this.lat + this.vel[1] * dt, -80, 80);
        this.vel[0] *= 0.92; this.vel[1] *= 0.92; this.dirty = true;
      } else if (this.spin && !this.pointers.size && !this.inside && !this.pickMode && now - this.lastUser > 3500) {
        this.lon += dt * 0.004 / this.zoom; this.dirty = true;
      }
      if (!this.visible || document.hidden) return;
      this._geom(); this._sg = this.selGroup();
      if (this.dirty) { this._base(); this.dirty = false; this.heatDirty = true; }
      const c = this.ctx, d = this.dpr;
      c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, this.cv.width, this.cv.height);
      c.drawImage(this.baseCv, 0, 0);
      c.setTransform(d, 0, 0, d, 0, 0);
      this._drawHeat(c, now); this._drawArcs(c, now); this._drawPoints(c, now);
    }
    _proj(x, y, z) {
      const M = this.M;
      return [M[0] * x + M[1] * y + M[2] * z, this.cx + this.R * (M[3] * x + M[4] * y + M[5] * z), this.cy - this.R * (M[6] * x + M[7] * y + M[8] * z)];
    }
    _base() {
      const c = this.bctx, W = world(), R = this.R, cx = this.cx, cy = this.cy;
      c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, this.baseCv.width, this.baseCv.height);
      c.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      let g = c.createRadialGradient(cx, cy, R * 0.96, cx, cy, R * 1.2);
      g.addColorStop(0, 'rgba(96,220,140,.26)'); g.addColorStop(1, 'rgba(96,220,140,0)');
      c.fillStyle = g; c.beginPath(); c.arc(cx, cy, R * 1.2, 0, TAU); c.fill();
      g = c.createRadialGradient(cx - R * .32, cy - R * .38, R * .04, cx, cy, R * 1.05);
      g.addColorStop(0, '#22603f'); g.addColorStop(.5, '#133c27'); g.addColorStop(1, '#081a10');
      c.fillStyle = g; c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.fill();
      if (W) {
        c.lineJoin = 'round'; c.lineCap = 'round';
        c.strokeStyle = 'rgba(170,230,190,.075)'; c.lineWidth = 1; c.beginPath(); W.grat.forEach(l => this._line(c, l)); c.stroke();
        c.fillStyle = 'rgba(150,220,175,.10)'; c.beginPath(); W.rings.forEach(r => { if (!r.polar) this._ring(c, r.p); }); c.fill();
        c.strokeStyle = 'rgba(214,242,222,.66)'; c.lineWidth = clamp(0.5 + this.zoom * 0.18, 0.6, 1.3); c.beginPath(); W.arcs.forEach(a => this._line(c, a)); c.stroke();
      }
      g = c.createRadialGradient(cx, cy, R * .72, cx, cy, R);
      g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,10,5,.34)');
      c.fillStyle = g; c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.fill();
      c.strokeStyle = 'rgba(170,235,195,.28)'; c.lineWidth = 1; c.beginPath(); c.arc(cx, cy, R, 0, TAU); c.stroke();
    }
    /* polilínea sobre la superficie: dibuja sólo la parte del hemisferio visible */
    _line(c, a) {
      const M = this.M, R = this.R, cx = this.cx, cy = this.cy, n = a.length / 3;
      let pd = 0, pu = 0, pw = 0;
      for (let i = 0; i < n; i++) {
        const x = a[3 * i], y = a[3 * i + 1], z = a[3 * i + 2];
        const d = M[0] * x + M[1] * y + M[2] * z, u = M[3] * x + M[4] * y, w = M[6] * x + M[7] * y + M[8] * z;
        if (i && (d > 0) !== (pd > 0)) {
          const t = pd / (pd - d); let iu = pu + (u - pu) * t, iw = pw + (w - pw) * t; const k = Math.hypot(iu, iw) || 1; iu /= k; iw /= k;
          if (pd > 0) c.lineTo(cx + R * iu, cy - R * iw); else { c.moveTo(cx + R * iu, cy - R * iw); c.lineTo(cx + R * u, cy - R * w); }
        } else if (d > 0) { if (i) c.lineTo(cx + R * u, cy - R * w); else c.moveTo(cx + R * u, cy - R * w); }
        pd = d; pu = u; pw = w;
      }
    }
    /* anillo relleno recortado al hemisferio visible (unido por el borde del disco) */
    _ring(c, a) {
      const M = this.M, R = this.R, cx = this.cx, cy = this.cy, n = a.length / 3;
      if (SD.length < n) { SD = new Float32Array(n * 2); SU = new Float32Array(n * 2); SW = new Float32Array(n * 2); }
      const D = SD, U = SU, Wv = SW; let vis = 0;
      for (let i = 0; i < n; i++) {
        const x = a[3 * i], y = a[3 * i + 1], z = a[3 * i + 2];
        D[i] = M[0] * x + M[1] * y + M[2] * z; U[i] = M[3] * x + M[4] * y; Wv[i] = M[6] * x + M[7] * y + M[8] * z; if (D[i] > 0) vis++;
      }
      if (!vis) return;
      if (vis === n) { c.moveTo(cx + R * U[0], cy - R * Wv[0]); for (let i = 1; i < n; i++) c.lineTo(cx + R * U[i], cy - R * Wv[i]); c.closePath(); return; }
      let s = -1; for (let i = 0; i < n; i++) if (D[i] > 0 && D[(i - 1 + n) % n] <= 0) { s = i; break; }
      if (s < 0) return;
      const cross = (i, j) => { const t = D[i] / (D[i] - D[j]); let u = U[i] + (U[j] - U[i]) * t, w = Wv[i] + (Wv[j] - Wv[i]) * t; const k = Math.hypot(u, w) || 1; return [u / k, w / k]; };
      let exitA = null;
      c.moveTo(cx + R * U[s], cy - R * Wv[s]);
      for (let k = 1; k <= n; k++) {
        const i = (s + k - 1) % n, j = (s + k) % n, vi = D[i] > 0, vj = D[j] > 0;
        if (vi && vj) c.lineTo(cx + R * U[j], cy - R * Wv[j]);
        else if (vi && !vj) { const p = cross(i, j); c.lineTo(cx + R * p[0], cy - R * p[1]); exitA = Math.atan2(p[1], p[0]); }
        else if (!vi && vj) {
          const p = cross(i, j), enA = Math.atan2(p[1], p[0]);
          if (exitA != null) { let dA = enA - exitA; while (dA > Math.PI) dA -= TAU; while (dA < -Math.PI) dA += TAU; const st = Math.max(1, Math.ceil(Math.abs(dA) / 0.08)); for (let q = 1; q <= st; q++) { const A = exitA + dA * q / st; c.lineTo(cx + R * Math.cos(A), cy - R * Math.sin(A)); } }
          c.lineTo(cx + R * p[0], cy - R * p[1]); if (k < n) c.lineTo(cx + R * U[j], cy - R * Wv[j]);
        }
      }
      c.closePath();
    }

    _dimmed(it) { return !!this.sel && this.sel !== it.id && !(it.group && it.group === this._sg); }
    selGroup() { const s = this.sel && this._find(this.sel); return s ? s.group : null; }

    /* Mapa de calor: se acumula la intensidad de cada punto (en una capa a media
       resolución) y luego se colorea con la escala verde → rojo. Así, varias
       ubicaciones cercanas suman calor en lugar de superponerse. Se recalcula
       sólo cuando cambia la vista o los datos. */
    _buildHeat() {
      const k = 0.5, W = Math.max(1, Math.ceil(this.w * k)), H = Math.max(1, Math.ceil(this.h * k));
      const hc = this.heatCv || (this.heatCv = document.createElement('canvas'));
      if (hc.width !== W || hc.height !== H) { hc.width = W; hc.height = H; }
      const c = hc.getContext('2d', { willReadFrequently: true }), R = this.R;
      c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, W, H); c.setTransform(k, 0, 0, k, 0, 0);
      c.globalCompositeOperation = 'lighter';
      let any = false;
      this.heat.forEach(h => {
        const [d, X, Y] = this._proj(h.v[0], h.v[1], h.v[2]); h._s = null;
        if (d < 0.04) return;
        const w = clamp(h.w, 0, 1), rad = (h.radius || (1.6 + 4.4 * w)) * D2R, rp = clamp(R * Math.sin(rad), 9, 26 + 46 * w);
        const a = (0.25 + 0.75 * w) * (this._dimmed(h) ? 0.35 : 1);
        const ang = Math.atan2(-(Y - this.cy), X - this.cx);
        c.save(); c.translate(X, Y); c.rotate(-ang); c.scale(Math.max(d, 0.1), 1);
        const g = c.createRadialGradient(0, 0, 0, 0, 0, rp);
        g.addColorStop(0, 'rgba(0,0,0,' + (a * 0.62).toFixed(3) + ')'); g.addColorStop(0.45, 'rgba(0,0,0,' + (a * 0.3).toFixed(3) + ')'); g.addColorStop(1, 'rgba(0,0,0,0)');
        c.fillStyle = g; c.beginPath(); c.arc(0, 0, rp, 0, TAU); c.fill(); c.restore();
        h._s = [X, Y, Math.max(8, rp * 0.55)]; any = true;
      });
      this.heatOn = any;
      if (!any) return;
      const img = c.getImageData(0, 0, W, H), px = img.data, lut = HEAT_LUT();
      for (let q = 3; q < px.length; q += 4) {
        const A = px[q]; if (!A) continue;
        const o = A * 4; px[q - 3] = lut[o]; px[q - 2] = lut[o + 1]; px[q - 1] = lut[o + 2]; px[q] = lut[o + 3];
      }
      c.putImageData(img, 0, 0);
    }
    _drawHeat(c, now) {
      if (!this.heat.length) { this.heatOn = false; return; }
      if (this.heatDirty) { this._buildHeat(); this.heatDirty = false; }
      if (!this.heatOn) return;
      c.save(); c.beginPath(); c.arc(this.cx, this.cy, this.R, 0, TAU); c.clip();
      c.globalAlpha = 0.86 + 0.14 * Math.sin(now * 0.003);
      c.drawImage(this.heatCv, 0, 0, this.w, this.h);
      c.restore();
    }
    _drawArcs(c, now) {
      const R = this.R, cx = this.cx, cy = this.cy, M = this.M;
      const order = this.arcs.slice().sort((a, b) => (a.id === this.sel) - (b.id === this.sel) || (a.pulse ? 1 : 0) - (b.pulse ? 1 : 0));
      c.lineCap = 'round'; c.lineJoin = 'round';
      order.forEach(a => {
        const n = a.n, sx = new Float32Array(n), sy = new Float32Array(n), vis = new Uint8Array(n); let any = 0;
        for (let i = 0; i < n; i++) {
          const x = a.p[3 * i], y = a.p[3 * i + 1], z = a.p[3 * i + 2];
          const d = M[0] * x + M[1] * y + M[2] * z, u = M[3] * x + M[4] * y, w = M[6] * x + M[7] * y + M[8] * z;
          sx[i] = cx + R * u; sy[i] = cy - R * w;
          vis[i] = d > 0 || (u * u + w * w) > 1 ? 1 : 0; any |= vis[i];
        }
        a._s = any ? { sx, sy, vis } : null;
        if (!any) return;
        const selected = this.sel === a.id, hot = this.hot === a, dim = this._dimmed(a);
        const col = a.color || '#8FD3A3', lw = (a.width || 1.6) * (selected || hot ? 1.8 : 1);
        const path = () => { c.beginPath(); let pen = false; for (let i = 0; i < n; i++) { if (vis[i]) { if (pen) c.lineTo(sx[i], sy[i]); else { c.moveTo(sx[i], sy[i]); pen = true; } } else pen = false; } };
        c.setLineDash(a.dash || []);
        if (a.pulse && !dim) {
          const k = 0.5 + 0.5 * Math.sin(now * 0.005 + a.ph * TAU);
          c.save(); c.shadowColor = rgba(col, 0.9); c.shadowBlur = 6 + 12 * k; c.strokeStyle = rgba(col, 0.35 + 0.35 * k); c.lineWidth = lw + 2.5 + 2 * k; path(); c.stroke(); c.restore();
        }
        c.strokeStyle = rgba(col, dim ? 0.14 : (selected || hot ? 1 : 0.82)); c.lineWidth = lw; path(); c.stroke();
        c.setLineDash([]);
        if (a.comet !== false && !dim) {
          const t = ((now / a.dur) + a.ph) % 1, head = Math.floor(t * (n - 1));
          for (let q = 0; q < 7; q++) {
            const i = head - q * Math.max(1, Math.round(n / 60)); if (i < 0 || !vis[i]) continue;
            c.fillStyle = rgba(q ? col : '#ffffff', (1 - q / 7) * 0.95);
            c.beginPath(); c.arc(sx[i], sy[i], Math.max(0.8, (lw * 0.9 + 1) * (1 - q / 9)), 0, TAU); c.fill();
          }
        }
        if (selected && a.label) { let i = Math.floor(n / 2); if (vis[i]) this._label(c, sx[i], sy[i] - 10, a.label); }
      });
    }
    _drawPoints(c, now) {
      if (!this.pts.length) return;
      const R = this.R;
      const list = this.pts.map(p => {
        const h = 1 + (p.h || 0) * 0.34, b = this._proj(p.v[0], p.v[1], p.v[2]), t = this._proj(p.v[0] * h, p.v[1] * h, p.v[2] * h);
        return { p, b, t };
      }).sort((a, b) => a.b[0] - b.b[0]);
      list.forEach(({ p, b, t }) => {
        p._s = null;
        const [d, bx, by] = b, [, tx, ty] = t;
        if (d < 0) return;
        const selected = this.sel === p.id, hot = this.hot === p, dim = this._dimmed(p);
        const col = selected ? (p.selColor || '#F2D14B') : (p.color || '#4ADE80');
        const al = dim ? 0.3 : 1, size = (p.size || 4.5) * (selected ? 1.55 : hot ? 1.25 : 1);
        if (d > 0) {
          const rp = Math.min(R * Math.sin((p.ring || 0.8) * D2R), 16) * (selected ? 2 : 1);
          const ang = Math.atan2(-(by - this.cy), bx - this.cx);
          c.save(); c.translate(bx, by); c.rotate(-ang); c.scale(Math.max(d, 0.1), 1);
          if (selected) { const g = c.createRadialGradient(0, 0, 0, 0, 0, rp * 1.4); g.addColorStop(0, rgba(col, .45)); g.addColorStop(1, rgba(col, 0)); c.fillStyle = g; c.beginPath(); c.arc(0, 0, rp * 1.4, 0, TAU); c.fill(); }
          c.strokeStyle = rgba(col, 0.75 * al); c.lineWidth = selected ? 2.4 : 1.6; c.beginPath(); c.arc(0, 0, Math.max(3, rp), 0, TAU); c.stroke();
          c.restore();
        }
        if (p.h > 0.002) {
          c.strokeStyle = rgba(col, 0.9 * al); c.lineWidth = selected ? 3.2 : 2.2; c.lineCap = 'round';
          c.beginPath(); c.moveTo(bx, by); c.lineTo(tx, ty); c.stroke();
        }
        if (p.pulse && !dim) { const k = (now * 0.0012 + (p.ph || 0)) % 1; c.strokeStyle = rgba(col, 0.6 * (1 - k)); c.lineWidth = 1.5; c.beginPath(); c.arc(tx, ty, size + 10 * k, 0, TAU); c.stroke(); }
        if (selected || hot) { c.save(); c.shadowColor = rgba(col, 0.9); c.shadowBlur = 14; }
        c.fillStyle = rgba(col, al); c.beginPath(); c.arc(tx, ty, size, 0, TAU); c.fill();
        if (selected || hot) c.restore();
        c.strokeStyle = rgba('#ffffff', 0.55 * al); c.lineWidth = 1; c.stroke();
        p._s = [tx, ty, size + 5];
        if ((selected || hot || p.showLabel) && p.label) this._label(c, tx, ty - size - 8, p.label);
      });
    }
    _label(c, x, y, text) {
      if (x < -20 || y < -20 || x > this.w + 20 || y > this.h + 40) return;
      c.save(); c.font = '600 11.5px Inter,system-ui,sans-serif';
      const w = c.measureText(text).width + 14, h = 20, lx = clamp(x - w / 2, 4, this.w - w - 4), ly = clamp(y - h, 4, this.h - h - 4);
      c.fillStyle = 'rgba(7,20,13,.9)'; c.strokeStyle = 'rgba(170,230,195,.35)'; c.lineWidth = 1;
      c.beginPath(); if (c.roundRect) c.roundRect(lx, ly, w, h, 6); else c.rect(lx, ly, w, h); c.fill(); c.stroke();
      c.fillStyle = '#EEF8F1'; c.textBaseline = 'middle'; c.fillText(text, lx + 7, ly + h / 2 + 0.5); c.restore();
    }

    /* ---------------------------- interacción --------------------------- */
    _pick(x, y) {
      let best = null, bd = 1e9;
      this.pts.forEach(p => { if (!p._s) return; const d = Math.hypot(x - p._s[0], y - p._s[1]); if (d < p._s[2] && d < bd) { bd = d; best = p; } });
      if (best) return best;
      bd = 7;
      this.arcs.forEach(a => {
        const s = a._s; if (!s) return;
        for (let i = 1; i < a.n; i++) {
          if (!s.vis[i] || !s.vis[i - 1]) continue;
          const x1 = s.sx[i - 1], y1 = s.sy[i - 1], dx = s.sx[i] - x1, dy = s.sy[i] - y1, L = dx * dx + dy * dy;
          const t = L ? clamp(((x - x1) * dx + (y - y1) * dy) / L, 0, 1) : 0, d = Math.hypot(x - x1 - t * dx, y - y1 - t * dy);
          if (d < bd) { bd = d; best = a; }
        }
      });
      if (best) return best;
      bd = 1e9;
      this.heat.forEach(h => { if (!h._s) return; const d = Math.hypot(x - h._s[0], y - h._s[1]); if (d < h._s[2] && d < bd) { bd = d; best = h; } });
      return best;
    }
    _latLonAt(x, y) {
      const u = (x - this.cx) / this.R, w = -(y - this.cy) / this.R, r2 = u * u + w * w;
      if (r2 > 1) return null;
      const d = Math.sqrt(1 - r2), M = this.M;
      const X = M[0] * d + M[3] * u + M[6] * w, Y = M[1] * d + M[4] * u + M[7] * w, Z = M[2] * d + M[5] * u + M[8] * w;
      return { lat: Math.asin(clamp(Z, -1, 1)) * R2D, lon: Math.atan2(Y, X) * R2D };
    }
    _tip(it, x, y) {
      const html = it && this.o.tooltip ? this.o.tooltip(it) : '';
      if (!html) { this.tip.classList.remove('on'); return; }
      this.tip.innerHTML = html; this.tip.classList.add('on');
      const tw = this.tip.offsetWidth, th = this.tip.offsetHeight;
      let lx = x + 14, ly = y + 14; if (lx + tw > this.w - 6) lx = x - tw - 14; if (ly + th > this.h - 6) ly = y - th - 14;
      this.tip.style.left = Math.max(6, lx) + 'px'; this.tip.style.top = Math.max(6, ly) + 'px';
    }
    _bind() {
      const cv = this.cv, pos = e => { const r = cv.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
      cv.addEventListener('pointerdown', e => {
        cv.setPointerCapture(e.pointerId); const p = pos(e);
        this.pointers.set(e.pointerId, p); this.down = { p, t: performance.now(), moved: false }; this.anim = null; this.vel = [0, 0]; this._lt = 0; this.lastUser = performance.now();
        if (this.pointers.size === 2) { const [a, b] = [...this.pointers.values()]; this.pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), z: this.zoom }; }
      });
      cv.addEventListener('pointermove', e => {
        const p = pos(e); this.inside = true;
        if (this.pointers.has(e.pointerId)) {
          const prev = this.pointers.get(e.pointerId); this.pointers.set(e.pointerId, p);
          if (this.pointers.size === 2 && this.pinch) { const [a, b] = [...this.pointers.values()]; this.zoom = clamp(this.pinch.z * Math.hypot(a[0] - b[0], a[1] - b[1]) / (this.pinch.d || 1), 0.8, 9); this.dirty = true; return; }
          const dx = p[0] - prev[0], dy = p[1] - prev[1];
          if (this.down && Math.hypot(p[0] - this.down.p[0], p[1] - this.down.p[1]) > 4) { this.down.moved = true; this.host.classList.add('drag'); this._tip(null); }
          if (this.down && this.down.moved) {
            const k = R2D / this.R; this.lon -= dx * k; this.lat = clamp(this.lat + dy * k, -80, 80);
            const dt = Math.max(12, e.timeStamp - (this._lt || e.timeStamp - 16)), vm = 0.05 / this.zoom; this.vel = [clamp(dx * k / dt, -vm, vm), clamp(dy * k / dt, -vm, vm)]; this._lt = e.timeStamp;
            this.dirty = true; this.lastUser = performance.now();
          }
          return;
        }
        const it = this._pick(p[0], p[1]);
        if (it !== this.hot) { this.hot = it; if (this.o.onHover) this.o.onHover(it); }
        this.host.classList.toggle('hot', !!it);
        this._tip(it, p[0], p[1]);
      });
      const up = e => {
        const wasClick = this.down && !this.down.moved && this.pointers.size === 1;
        this.pointers.delete(e.pointerId); if (this.pointers.size < 2) this.pinch = null;
        if (!this._lt || e.timeStamp - this._lt > 90) this.vel = [0, 0];
        this.host.classList.remove('drag');
        if (wasClick && e.type === 'pointerup') {
          const p = pos(e);
          if (this.pickMode && this.o.onPick) { const ll = this._latLonAt(p[0], p[1]); if (ll) this.o.onPick(ll); }
          else { const it = this._pick(p[0], p[1]); if (this.o.onClick) this.o.onClick(it || null); }
        }
        if (!this.pointers.size) this.down = null;
        this.lastUser = performance.now();
      };
      cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
      cv.addEventListener('pointerleave', () => { this.inside = false; if (this.hot) { this.hot = null; if (this.o.onHover) this.o.onHover(null); } this.host.classList.remove('hot'); this._tip(null); });
      cv.addEventListener('dblclick', e => { const p = pos(e), ll = this._latLonAt(p[0], p[1]); if (ll) this.flyTo(ll.lat, ll.lon, clamp(this.zoom * 1.7, 0.8, 9), 600); });
      cv.addEventListener('wheel', e => {
        if (!(e.ctrlKey || e.metaKey)) { this.hint.classList.add('on'); clearTimeout(this._ht); this._ht = setTimeout(() => this.hint.classList.remove('on'), 1300); return; }
        e.preventDefault(); this.anim = null; this.zoom = clamp(this.zoom * Math.exp(-e.deltaY * 0.0018), 0.8, 9); this.dirty = true; this.lastUser = performance.now();
      }, { passive: false });
      cv.tabIndex = 0;
      cv.addEventListener('keydown', e => {
        const k = { ArrowLeft: [-8, 0], ArrowRight: [8, 0], ArrowUp: [0, 6], ArrowDown: [0, -6] }[e.key];
        if (k) { e.preventDefault(); this.flyTo(this.lat + k[1], this.lon + k[0], this.zoom, 300); }
        else if (e.key === '+' || e.key === '=') this.zoomBy(1.45); else if (e.key === '-') this.zoomBy(1 / 1.45);
      });
    }
  }

  /* ======================= nomenclátor de ubicaciones ===================== */
  /* Ciudades y países frecuentes en operaciones y viajes de la región. Sirve
     para ubicar en el globo textos como «Vuelo — Buenos Aires → Houston» o
     «Planta Córdoba» cuando el dato no trae coordenadas. [nombre, lat, lon, país, alias…] */
  const CITIES = [
    ['Buenos Aires', -34.60, -58.38, 'AR', 'caba', 'capital federal', 'ciudad autonoma de buenos aires', 'ezeiza', 'aeroparque', 'bs as', 'bsas', 'amba'],
    ['Córdoba', -31.42, -64.19, 'AR', 'cordoba'], ['Rosario', -32.95, -60.66, 'AR'], ['Mendoza', -32.89, -68.84, 'AR'],
    ['Neuquén', -38.95, -68.06, 'AR', 'neuquen'], ['Comodoro Rivadavia', -45.86, -67.48, 'AR', 'comodoro'], ['Bahía Blanca', -38.72, -62.27, 'AR', 'bahia blanca'],
    ['Mar del Plata', -38.00, -57.56, 'AR'], ['San Miguel de Tucumán', -26.81, -65.22, 'AR', 'tucuman'], ['Salta', -24.79, -65.41, 'AR'],
    ['Santa Fe', -31.63, -60.70, 'AR'], ['Río Gallegos', -51.62, -69.22, 'AR', 'rio gallegos'], ['Ushuaia', -54.80, -68.30, 'AR'],
    ['San Carlos de Bariloche', -41.13, -71.31, 'AR', 'bariloche'], ['Posadas', -27.37, -55.90, 'AR'], ['Resistencia', -27.45, -58.99, 'AR'],
    ['Corrientes', -27.47, -58.83, 'AR'], ['San Juan', -31.54, -68.54, 'AR'], ['San Luis', -33.30, -66.34, 'AR'], ['San Salvador de Jujuy', -24.19, -65.30, 'AR', 'jujuy'],
    ['Paraná', -31.73, -60.53, 'AR', 'parana'], ['La Plata', -34.92, -57.95, 'AR'], ['Trelew', -43.25, -65.31, 'AR'], ['Puerto Madryn', -42.77, -65.04, 'AR'],
    ['Río Grande', -53.79, -67.71, 'AR', 'rio grande'], ['Santa Rosa', -36.62, -64.29, 'AR'], ['Viedma', -40.81, -62.99, 'AR'], ['Santiago del Estero', -27.78, -64.26, 'AR'],
    ['Campana', -34.17, -58.96, 'AR'], ['Zárate', -34.10, -59.03, 'AR', 'zarate'], ['San Nicolás', -33.33, -60.22, 'AR', 'san nicolas'], ['Pilar', -34.46, -58.91, 'AR'],
    ['Bragado', -35.12, -60.49, 'AR'], ['Vicente López', -34.53, -58.48, 'AR', 'vicente lopez'], ['Rafaela', -31.25, -61.49, 'AR'], ['Río Cuarto', -33.13, -64.35, 'AR', 'rio cuarto'],
    ['Villa María', -32.41, -63.24, 'AR', 'villa maria'], ['Tandil', -37.32, -59.13, 'AR'], ['Olavarría', -36.89, -60.32, 'AR', 'olavarria'], ['Añelo', -38.35, -68.79, 'AR', 'anelo', 'vaca muerta'],
    ['Caleta Olivia', -46.44, -67.52, 'AR'], ['San Lorenzo', -32.75, -60.73, 'AR'], ['Villa Mercedes', -33.68, -65.46, 'AR'], ['General Pacheco', -34.46, -58.64, 'AR', 'pacheco'],
    ['Montevideo', -34.90, -56.19, 'UY', 'carrasco'], ['Canelones', -34.52, -56.28, 'UY'], ['Punta del Este', -34.96, -54.94, 'UY'], ['Colonia del Sacramento', -34.47, -57.84, 'UY', 'colonia del sacramento', 'colonia uruguay'],
    ['Paysandú', -32.32, -58.08, 'UY', 'paysandu'], ['Salto', -31.39, -57.96, 'UY'], ['Fray Bentos', -33.13, -58.30, 'UY'], ['Rivera', -30.90, -55.55, 'UY'], ['Maldonado', -34.90, -54.95, 'UY'],
    ['São Paulo', -23.55, -46.63, 'BR', 'sao paulo', 'san pablo', 'guarulhos'], ['Río de Janeiro', -22.91, -43.17, 'BR', 'rio de janeiro'], ['Brasilia', -15.79, -47.88, 'BR'],
    ['Belo Horizonte', -19.92, -43.94, 'BR'], ['Porto Alegre', -30.03, -51.23, 'BR'], ['Curitiba', -25.43, -49.27, 'BR'], ['Campinas', -22.91, -47.06, 'BR'], ['Santos', -23.96, -46.33, 'BR'],
    ['Salvador de Bahía', -12.97, -38.50, 'BR', 'salvador', 'salvador de bahia'], ['Recife', -8.05, -34.88, 'BR'], ['Fortaleza', -3.73, -38.52, 'BR'], ['Manaos', -3.12, -60.02, 'BR', 'manaus'],
    ['Florianópolis', -27.60, -48.55, 'BR', 'florianopolis'], ['Joinville', -26.30, -48.85, 'BR'], ['Vitória', -20.32, -40.34, 'BR', 'vitoria'], ['Goiânia', -16.69, -49.26, 'BR', 'goiania'], ['Belém', -1.46, -48.49, 'BR', 'belem'],
    ['Santiago de Chile', -33.45, -70.67, 'CL', 'santiago'], ['Valparaíso', -33.05, -71.62, 'CL', 'valparaiso'], ['Antofagasta', -23.65, -70.40, 'CL'], ['Concepción', -36.83, -73.05, 'CL', 'concepcion'],
    ['Calama', -22.46, -68.93, 'CL'], ['Iquique', -20.21, -70.15, 'CL'], ['Puerto Montt', -41.47, -72.94, 'CL'], ['Punta Arenas', -53.16, -70.91, 'CL'], ['La Serena', -29.90, -71.25, 'CL'],
    ['Asunción', -25.26, -57.58, 'PY', 'asuncion'], ['Ciudad del Este', -25.51, -54.61, 'PY'], ['Encarnación', -27.33, -55.87, 'PY', 'encarnacion'],
    ['La Paz', -16.50, -68.15, 'BO'], ['Santa Cruz de la Sierra', -17.78, -63.18, 'BO', 'santa cruz de la sierra'], ['Cochabamba', -17.39, -66.16, 'BO'],
    ['Lima', -12.05, -77.04, 'PE', 'callao'], ['Arequipa', -16.41, -71.54, 'PE'], ['Cusco', -13.53, -71.97, 'PE', 'cuzco'], ['Quito', -0.18, -78.47, 'EC'], ['Guayaquil', -2.19, -79.89, 'EC'],
    ['Bogotá', 4.71, -74.07, 'CO', 'bogota'], ['Medellín', 6.24, -75.58, 'CO', 'medellin'], ['Cali', 3.45, -76.53, 'CO'], ['Barranquilla', 10.97, -74.80, 'CO'], ['Cartagena de Indias', 10.39, -75.51, 'CO', 'cartagena'],
    ['Caracas', 10.49, -66.88, 'VE'], ['Ciudad de Panamá', 8.98, -79.52, 'PA', 'ciudad de panama', 'panama', 'tocumen'], ['San José de Costa Rica', 9.93, -84.08, 'CR', 'san jose de costa rica', 'san jose costa rica', 'costa rica'],
    ['Ciudad de Guatemala', 14.63, -90.51, 'GT', 'ciudad de guatemala', 'guatemala'], ['San Salvador', 13.69, -89.22, 'SV'], ['Tegucigalpa', 14.07, -87.19, 'HN'], ['Managua', 12.11, -86.24, 'NI'],
    ['Santo Domingo', 18.49, -69.93, 'DO'], ['La Habana', 23.11, -82.37, 'CU', 'habana', 'havana'], ['San Juan de Puerto Rico', 18.47, -66.11, 'PR', 'san juan de puerto rico', 'puerto rico'],
    ['Ciudad de México', 19.43, -99.13, 'MX', 'ciudad de mexico', 'cdmx', 'mexico df', 'mexico city', 'mexico'], ['Monterrey', 25.69, -100.32, 'MX'], ['Guadalajara', 20.67, -103.35, 'MX'],
    ['Cancún', 21.16, -86.85, 'MX', 'cancun'], ['Tijuana', 32.51, -117.04, 'MX'], ['Querétaro', 20.59, -100.39, 'MX', 'queretaro'], ['Puebla', 19.04, -98.21, 'MX'],
    ['Nueva York', 40.71, -74.01, 'US', 'nueva york', 'new york', 'nyc', 'jfk', 'newark'], ['Houston', 29.76, -95.37, 'US'], ['Miami', 25.76, -80.19, 'US'], ['Los Ángeles', 34.05, -118.24, 'US', 'los angeles'],
    ['San Francisco', 37.77, -122.42, 'US'], ['Chicago', 41.88, -87.63, 'US'], ['Dallas', 32.78, -96.80, 'US'], ['Atlanta', 33.75, -84.39, 'US'], ['Washington', 38.91, -77.04, 'US', 'washington dc'],
    ['Boston', 42.36, -71.06, 'US'], ['Seattle', 47.61, -122.33, 'US'], ['Denver', 39.74, -104.99, 'US'], ['Orlando', 28.54, -81.38, 'US'], ['Las Vegas', 36.17, -115.14, 'US'],
    ['Detroit', 42.33, -83.05, 'US'], ['Phoenix', 33.45, -112.07, 'US'], ['Nueva Orleans', 29.95, -90.07, 'US', 'nueva orleans', 'new orleans'], ['Pittsburgh', 40.44, -79.99, 'US'],
    ['Toronto', 43.65, -79.38, 'CA'], ['Montreal', 45.50, -73.57, 'CA'], ['Vancouver', 49.28, -123.12, 'CA'], ['Calgary', 51.05, -114.07, 'CA'], ['Ottawa', 45.42, -75.70, 'CA'],
    ['Madrid', 40.42, -3.70, 'ES', 'barajas'], ['Barcelona', 41.39, 2.17, 'ES'], ['Valencia', 39.47, -0.38, 'ES'], ['Sevilla', 37.39, -5.98, 'ES'], ['Bilbao', 43.26, -2.93, 'ES'],
    ['Lisboa', 38.72, -9.14, 'PT', 'lisbon'], ['Oporto', 41.15, -8.61, 'PT', 'porto'], ['París', 48.86, 2.35, 'FR', 'paris'], ['Lyon', 45.76, 4.84, 'FR'], ['Marsella', 43.30, 5.37, 'FR', 'marseille'],
    ['Londres', 51.51, -0.13, 'GB', 'london', 'heathrow'], ['Manchester', 53.48, -2.24, 'GB'], ['Edimburgo', 55.95, -3.19, 'GB', 'edinburgh'], ['Dublín', 53.35, -6.26, 'IE', 'dublin'],
    ['Fráncfort', 50.11, 8.68, 'DE', 'francfort', 'frankfurt'], ['Múnich', 48.14, 11.58, 'DE', 'munich', 'munchen'], ['Berlín', 52.52, 13.40, 'DE', 'berlin'], ['Hamburgo', 53.55, 9.99, 'DE', 'hamburg'],
    ['Düsseldorf', 51.23, 6.77, 'DE', 'dusseldorf'], ['Colonia (Alemania)', 50.94, 6.96, 'DE', 'koln', 'cologne'], ['Stuttgart', 48.78, 9.18, 'DE'],
    ['Ámsterdam', 52.37, 4.90, 'NL', 'amsterdam'], ['Róterdam', 51.92, 4.48, 'NL', 'rotterdam', 'roterdam'], ['Bruselas', 50.85, 4.35, 'BE', 'brussels'], ['Amberes', 51.22, 4.40, 'BE', 'antwerp'],
    ['Zúrich', 47.38, 8.54, 'CH', 'zurich'], ['Ginebra', 46.20, 6.14, 'CH', 'geneva'], ['Milán', 45.46, 9.19, 'IT', 'milan'], ['Roma', 41.90, 12.50, 'IT', 'rome'], ['Turín', 45.07, 7.69, 'IT', 'turin'],
    ['Génova', 44.41, 8.93, 'IT', 'genova', 'genoa'], ['Viena', 48.21, 16.37, 'AT', 'vienna', 'wien'], ['Praga', 50.08, 14.44, 'CZ', 'prague'], ['Varsovia', 52.23, 21.01, 'PL', 'warsaw'],
    ['Estocolmo', 59.33, 18.07, 'SE', 'stockholm'], ['Oslo', 59.91, 10.75, 'NO'], ['Copenhague', 55.68, 12.57, 'DK', 'copenhagen'], ['Helsinki', 60.17, 24.94, 'FI'],
    ['Atenas', 37.98, 23.73, 'GR', 'athens'], ['Estambul', 41.01, 28.98, 'TR', 'istanbul'], ['Moscú', 55.76, 37.62, 'RU', 'moscu', 'moscow'], ['Budapest', 47.50, 19.04, 'HU'], ['Bucarest', 44.43, 26.10, 'RO', 'bucharest'],
    ['Tokio', 35.68, 139.69, 'JP', 'tokyo'], ['Osaka', 34.69, 135.50, 'JP'], ['Seúl', 37.57, 126.98, 'KR', 'seul', 'seoul'], ['Pekín', 39.90, 116.41, 'CN', 'pekin', 'beijing'],
    ['Shanghái', 31.23, 121.47, 'CN', 'shanghai'], ['Shenzhen', 22.54, 114.06, 'CN'], ['Cantón', 23.13, 113.26, 'CN', 'canton', 'guangzhou'], ['Hong Kong', 22.32, 114.17, 'HK'],
    ['Taipéi', 25.03, 121.57, 'TW', 'taipei'], ['Singapur', 1.35, 103.82, 'SG', 'singapore'], ['Bangkok', 13.76, 100.50, 'TH'], ['Kuala Lumpur', 3.14, 101.69, 'MY'], ['Yakarta', -6.21, 106.85, 'ID', 'jakarta'],
    ['Manila', 14.60, 120.98, 'PH'], ['Ciudad Ho Chi Minh', 10.82, 106.63, 'VN', 'ho chi minh', 'saigon'], ['Hanói', 21.03, 105.85, 'VN', 'hanoi'], ['Nueva Delhi', 28.61, 77.21, 'IN', 'nueva delhi', 'new delhi', 'delhi'],
    ['Bombay', 19.08, 72.88, 'IN', 'mumbai'], ['Bangalore', 12.97, 77.59, 'IN', 'bengaluru'], ['Chennai', 13.08, 80.27, 'IN'], ['Dubái', 25.20, 55.27, 'AE', 'dubai'], ['Abu Dabi', 24.45, 54.38, 'AE', 'abu dhabi'],
    ['Doha', 25.29, 51.53, 'QA'], ['Riad', 24.71, 46.68, 'SA', 'riyadh'], ['Tel Aviv', 32.09, 34.78, 'IL'], ['Karachi', 24.86, 67.01, 'PK'],
    ['Johannesburgo', -26.20, 28.05, 'ZA', 'johannesburg'], ['Ciudad del Cabo', -33.92, 18.42, 'ZA', 'cape town'], ['El Cairo', 30.04, 31.24, 'EG', 'cairo'], ['Lagos', 6.52, 3.38, 'NG'],
    ['Nairobi', -1.29, 36.82, 'KE'], ['Casablanca', 33.57, -7.59, 'MA'], ['Luanda', -8.84, 13.23, 'AO'],
    ['Sídney', -33.87, 151.21, 'AU', 'sidney', 'sydney'], ['Melbourne', -37.81, 144.96, 'AU'], ['Perth', -31.95, 115.86, 'AU'], ['Brisbane', -27.47, 153.03, 'AU'], ['Auckland', -36.85, 174.76, 'NZ']
  ];
  const COUNTRIES = {
    AR: ['Argentina', -34.0, -64.0], UY: ['Uruguay', -32.8, -56.0], BR: ['Brasil', -10.0, -52.0, 'brazil'], CL: ['Chile', -33.5, -70.8], PY: ['Paraguay', -23.4, -58.4], BO: ['Bolivia', -16.7, -64.7],
    PE: ['Perú', -9.2, -75.0, 'peru'], EC: ['Ecuador', -1.8, -78.2], CO: ['Colombia', 4.6, -74.1], VE: ['Venezuela', 7.1, -66.2], MX: ['México', 23.6, -102.5, 'mexico'], US: ['Estados Unidos', 39.8, -98.6, 'usa', 'eeuu', 'united states'],
    CA: ['Canadá', 56.1, -106.3, 'canada'], ES: ['España', 40.4, -3.7, 'espana', 'spain'], PT: ['Portugal', 39.4, -8.2], FR: ['Francia', 46.2, 2.2, 'france'], DE: ['Alemania', 51.2, 10.4, 'germany'],
    IT: ['Italia', 41.9, 12.6, 'italy'], GB: ['Reino Unido', 54.0, -2.0, 'reino unido', 'united kingdom', 'uk', 'inglaterra'], NL: ['Países Bajos', 52.1, 5.3, 'paises bajos', 'holanda', 'netherlands'],
    BE: ['Bélgica', 50.5, 4.5, 'belgica'], CH: ['Suiza', 46.8, 8.2, 'switzerland'], AT: ['Austria', 47.5, 14.6], SE: ['Suecia', 60.1, 18.6], NO: ['Noruega', 60.5, 8.5], DK: ['Dinamarca', 56.3, 9.5],
    FI: ['Finlandia', 61.9, 25.7], IE: ['Irlanda', 53.4, -8.2], PL: ['Polonia', 51.9, 19.1], CZ: ['Chequia', 49.8, 15.5], CN: ['China', 35.9, 104.2], JP: ['Japón', 36.2, 138.3, 'japon', 'japan'],
    KR: ['Corea del Sur', 35.9, 127.8, 'corea'], IN: ['India', 20.6, 79.0], SG: ['Singapur', 1.35, 103.8], AE: ['Emiratos Árabes Unidos', 23.4, 53.8, 'emiratos'], SA: ['Arabia Saudita', 23.9, 45.1],
    ZA: ['Sudáfrica', -30.6, 22.9, 'sudafrica'], AU: ['Australia', -25.3, 133.8], NZ: ['Nueva Zelanda', -40.9, 174.9], TR: ['Turquía', 39.0, 35.2, 'turquia'], RU: ['Rusia', 61.5, 105.3],
    IL: ['Israel', 31.0, 34.9], EG: ['Egipto', 26.8, 30.8], NG: ['Nigeria', 9.1, 8.7], KE: ['Kenia', -0.02, 37.9], MA: ['Marruecos', 31.8, -7.1], PA: ['Panamá', 8.5, -80.8],
    CR: ['Costa Rica', 9.7, -83.8], GT: ['Guatemala', 15.8, -90.2], DO: ['República Dominicana', 18.7, -70.2, 'republica dominicana'], CU: ['Cuba', 21.5, -77.8], HN: ['Honduras', 15.2, -86.2],
    SV: ['El Salvador', 13.8, -88.9], NI: ['Nicaragua', 12.9, -85.2], TH: ['Tailandia', 15.9, 100.99], MY: ['Malasia', 4.2, 101.98], ID: ['Indonesia', -0.8, 113.9], PH: ['Filipinas', 12.9, 121.8],
    VN: ['Vietnam', 14.1, 108.3], TW: ['Taiwán', 23.7, 121.0, 'taiwan'], HK: ['Hong Kong', 22.3, 114.2], QA: ['Catar', 25.35, 51.2, 'qatar'], PK: ['Pakistán', 30.4, 69.3, 'pakistan'],
    GR: ['Grecia', 39.1, 21.8], HU: ['Hungría', 47.2, 19.5, 'hungria'], RO: ['Rumania', 45.9, 24.97], AO: ['Angola', -11.2, 17.9], PR: ['Puerto Rico', 18.2, -66.5]
  };
  const norm = s => ' ' + String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, ' ').trim() + ' ';
  let INDEX = null;
  function index() {
    if (INDEX) return INDEX;
    INDEX = [];
    CITIES.forEach(([name, lat, lon, cc, ...al]) => [name, ...al].forEach(a => INDEX.push({ key: norm(a), name, lat, lon, cc, kind: 'city' })));
    Object.entries(COUNTRIES).forEach(([cc, [name, lat, lon, ...al]]) => [name, ...al].forEach(a => INDEX.push({ key: norm(a), name, lat, lon, cc, kind: 'country' })));
    return INDEX;
  }
  /* Busca la ubicación más específica mencionada en el texto. hint = código de país preferido. */
  function geocode(text, hint) {
    const t = norm(text); if (t.trim() === '') return null;
    let best = null;
    index().forEach(e => {
      if (!t.includes(e.key)) return;
      const score = (e.kind === 'city' ? 1000 : 0) + e.key.length + (hint && e.cc === hint ? 500 : 0);
      if (!best || score > best.score) best = Object.assign({ score }, e);
    });
    return best ? { name: best.name, lat: best.lat, lon: best.lon, cc: best.cc, kind: best.kind } : null;
  }
  function country(code) { const c = COUNTRIES[String(code || '').toUpperCase()]; return c ? { name: c[0], lat: c[1], lon: c[2], cc: String(code).toUpperCase(), kind: 'country' } : null; }
  /* «Vuelo — Buenos Aires → Houston» → [lugar de origen, lugar de destino] */
  function parseRoute(text) {
    let s = String(text || '');
    const dash = s.search(/\s[—–]\s/); if (dash >= 0 && /→|->|>/.test(s.slice(dash))) s = s.slice(dash + 3);
    let parts = s.split(/\s*(?:→|->|=>|›|»|>)\s*/).filter(x => x.trim());
    if (parts.length < 2) { const m = s.match(/(?:^|\s)(?:de|desde)\s+(.+?)\s+(?:a|hasta|hacia)\s+(.+)$/i); if (m) parts = [m[1], m[2]]; }
    if (parts.length < 2) return null;
    const a = geocode(parts[0]), b = geocode(parts[parts.length - 1]);
    return a && b && (a.lat !== b.lat || a.lon !== b.lon) ? [a, b] : null;
  }
  /* distancia de gran círculo en km */
  function distanceKm(a, b) { return angle(vec(a[0], a[1]), vec(b[0], b[1])) * 6371; }

  window.IVZGlobe = { create: (host, o) => new Globe(host, o), ramp, geocode, country, parseRoute, distanceKm, normalize: norm };
})();
