/* The users of a company: list them, add one, change its role, switch it off or give it a new password.
   Shared by IVZ Sustainability Hub and IVZ Carbon (copy in carbon/static), in the client's account settings
   and in Administración. The server decides who may do what; this only draws and calls it. */
window.IVZUsers = (() => {
  const ROLES = {admin: 'Administrador', editor: 'Editor', viewer: 'Lector'};
  const HELP = 'Administrador: todo, incluidos los usuarios · Editor: carga datos, genera reportes y cierra años · Lector: solo mira y descarga.';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const line = 'var(--line, #dfe5e1)', muted = 'var(--muted, var(--ink-2, #5f6b66))', bad = 'var(--bad, var(--danger, #b42318))';
  const control = 'font:inherit;font-size:13px;padding:6px 8px;border-radius:7px;border:1px solid ' + line + ';background:#fff;color:inherit';
  const button = control + ';cursor:pointer';
  const roleSelect = (value, attrs = '') => '<select ' + attrs + ' style="' + control + '">' +
    Object.entries(ROLES).map(([k, v]) => '<option value="' + k + '"' + (k === value ? ' selected' : '') + '>' + v + '</option>').join('') + '</select>';

  /* base: '/api/users' or '/api/admin/accounts/<id>/users'; headers: the app's request header;
     me: the signed-in username, marked as «vos». */
  function mount(box, {base, headers, me = ''}) {
    async function call(path = '', method = 'GET', body) {
      const r = await fetch(base + path, {method, credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json', ...headers}, body: body === undefined ? undefined : JSON.stringify(body)});
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'No se pudo completar la solicitud (' + r.status + ')');
      return data;
    }
    const say = (text, failed = true) => { const p = box.querySelector('[data-u-msg]'); p.style.color = failed ? bad : muted; p.textContent = text; };
    const reveal = (username, password) => {
      const p = box.querySelector('[data-u-pw]');
      p.hidden = false;
      p.innerHTML = 'Contraseña de <b>' + esc(username) + '</b> — copiala ahora, no se va a poder ver de nuevo:' +
        '<code style="display:block;margin:6px 0;font:600 14px ui-monospace,monospace;word-break:break-all">' + esc(password) + '</code>' +
        '<button type="button" data-u-copy style="' + button + '">Copiar</button>';
      p.querySelector('[data-u-copy]').onclick = () => navigator.clipboard.writeText(password).catch(() => {});
    };
    function render(list) {
      box.innerHTML = '<p style="margin:0 0 8px;font-size:12.5px;color:' + muted + '">' + HELP + '</p>' +
        '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">' + list.map(u =>
          '<tr data-u="' + esc(u.id) + '" style="border-top:1px solid ' + line + (u.active ? '' : ';opacity:.6') + '">' +
          '<td style="padding:7px 6px 7px 0"><b>' + esc(u.username) + '</b>' + (u.username === me ? ' <span style="color:' + muted + '">(vos)</span>' : '') +
          (u.active ? '' : '<br><small style="color:' + bad + '">Desactivado</small>') + '</td>' +
          '<td style="padding:7px 6px">' + roleSelect(u.role, 'data-u-role aria-label="Rol de ' + esc(u.username) + '"') + '</td>' +
          '<td style="padding:7px 0;text-align:right;white-space:nowrap"><button type="button" data-u-reset style="' + button + '">Nueva contraseña</button> ' +
          '<button type="button" data-u-active="' + (u.active ? 0 : 1) + '" style="' + button + (u.active ? ';color:' + bad : '') + '">' + (u.active ? 'Desactivar' : 'Reactivar') + '</button></td></tr>').join('') +
        '</table></div>' +
        '<form data-u-add style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">' +
        '<input name="username" required maxlength="150" placeholder="Nuevo usuario" autocomplete="off" style="' + control + ';flex:1;min-width:140px">' +
        roleSelect('editor', 'name="role" aria-label="Rol del nuevo usuario"') + '<button type="submit" style="' + button + '">Agregar usuario</button></form>' +
        '<div data-u-pw hidden style="margin-top:10px;padding:10px 12px;border-radius:8px;background:var(--accent-soft, #e7f1e8);font-size:13px"></div>' +
        '<p data-u-msg role="alert" style="min-height:18px;margin:6px 0 0;font-size:13px"></p>';
      box.querySelector('[data-u-add]').onsubmit = async e => {
        e.preventDefault();
        const form = e.target, add = form.querySelector('button');
        add.disabled = true;
        try {
          const made = await call('', 'POST', {username: form.username.value, role: form.role.value});
          await load(); reveal(made.username, made.password);
        } catch (err) { say(err.message); add.disabled = false; }
      };
      box.querySelectorAll('tr[data-u]').forEach(row => {
        const id = row.dataset.u, user = list.find(u => u.id === id);
        row.querySelector('[data-u-role]').onchange = async e => {
          try { await call('/' + encodeURIComponent(id), 'PUT', {role: e.target.value}); say(user.username + ' ahora es ' + ROLES[e.target.value] + '.', false); user.role = e.target.value; }
          catch (err) { e.target.value = user.role; say(err.message); }
        };
        row.querySelector('[data-u-active]').onclick = async e => {
          const active = e.currentTarget.dataset.uActive === '1';
          if (!active && !confirm('¿Desactivar a ' + user.username + '? Se cierran sus sesiones y no puede volver a entrar hasta que lo reactives.')) return;
          try { await call('/' + encodeURIComponent(id), 'PUT', {active}); await load(); } catch (err) { say(err.message); }
        };
        row.querySelector('[data-u-reset]').onclick = async () => {
          if (!confirm('¿Generar una nueva contraseña para ' + user.username + '? La actual deja de funcionar y se cierran sus sesiones.')) return;
          try { const r = await call('/' + encodeURIComponent(id) + '/reset-password', 'POST'); reveal(user.username, r.password); } catch (err) { say(err.message); }
        };
      });
    }
    async function load() { render(await call()); }
    box.innerHTML = '<p style="color:' + muted + ';font-size:13px">Cargando usuarios…</p>';
    return load().catch(err => { box.innerHTML = '<p style="color:' + bad + ';font-size:13px">' + esc(err.message) + '</p>'; });
  }
  return {mount, ROLES};
})();
