'use strict';
// Studio-Einrichtung – Oberfläche des lokalen Assistenten (setup.py). Kein Framework, keine
// Inline-Handler (CSP). Alles, was von außen kommt, geht durch esc().

const $ = (sel, wurzel = document) => wurzel.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(pfad, daten) {
  const r = await fetch(pfad, {
    method: daten === undefined ? 'GET' : 'POST',
    headers: { 'X-Studio-Setup': '1', 'Content-Type': 'application/json' },
    body: daten === undefined ? undefined : JSON.stringify(daten),
    credentials: 'same-origin',
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `Fehler ${r.status}`);
  return j;
}

const SCHRITTE = [
  { id: 'passwort', titel: 'PC-Passwort', nurImage: true },
  { id: 'internet', titel: 'Internet' },
  { id: 'docker', titel: 'Docker' },
  { id: 'lizenz', titel: 'Lizenz' },
  { id: 'fernwartung', titel: 'Fernwartung' },
  { id: 'konfiguration', titel: 'Konfiguration' },
  { id: 'installation', titel: 'Installation' },
  { id: 'fertig', titel: 'Fertig' },
];

let stand = null;          // /api/status
let internetOk = false;    // wird bei jedem Öffnen neu geprüft
let aktuell = null;        // angezeigter Schritt
let umfrage = null;        // Timer der Protokoll-Abfrage

function erledigt(id) {
  if (!stand) return false;
  // Nach der Installation sind die Zwischenstände gelöscht – dann ist schlicht alles erledigt.
  if (stand.installiert) return true;
  switch (id) {
    case 'passwort': return stand.passwort_gesetzt;
    case 'internet': return internetOk;
    case 'docker': return stand.docker;
    case 'lizenz': return !!stand.aktivierung;
    case 'fernwartung': return !!stand.fernwartung;
    case 'konfiguration': return !!stand.konfiguration;
    case 'installation': return !!stand.installiert;
    default: return false;
  }
}

function sichtbar() { return SCHRITTE.filter((s) => !s.nurImage || stand?.ist_image); }

function naechster() {
  if (stand?.installiert) return 'fertig';
  return (sichtbar().find((s) => s.id !== 'fertig' && !erledigt(s.id)) || { id: 'installation' }).id;
}

function erreichbar(id) {
  const liste = sichtbar();
  const bis = liste.findIndex((s) => s.id === naechster());
  return liste.findIndex((s) => s.id === id) <= bis;
}

async function neuLaden(ziel) {
  stand = await api('/api/status');
  $('#cloud').textContent = stand.cloud;
  zeigen(ziel || naechster());
}

function seitenleiste() {
  $('#schritte').innerHTML = sichtbar().map((s, i) => {
    const klassen = [s.id === aktuell ? 'aktiv' : '', erledigt(s.id) ? 'erledigt' : '', erreichbar(s.id) ? 'erreichbar' : ''].join(' ');
    return `<li class="${klassen}" data-schritt="${s.id}"><span class="punkt">${erledigt(s.id) ? '✓' : i + 1}</span>${esc(s.titel)}</li>`;
  }).join('');
  document.querySelectorAll('#schritte li.erreichbar').forEach((li) =>
    li.addEventListener('click', () => { if (!stand?.installiert || li.dataset.schritt === 'fertig') zeigen(li.dataset.schritt); }));
}

function zeigen(id) {
  if (umfrage) { clearInterval(umfrage); umfrage = null; }
  aktuell = id;
  seitenleiste();
  ({ passwort, internet, docker, lizenz, fernwartung, konfiguration, installation, fertig }[id])();
}

function fehlerZeigen(e, wo = '#meldung') {
  const ziel = $(wo);
  if (ziel) ziel.innerHTML = `<div class="box fehler">${esc(e.message || e)}</div>`;
}

function knopf(sel, arbeit) {
  const k = $(sel);
  if (!k) return;
  k.addEventListener('click', async () => {
    k.disabled = true;
    $('#meldung') && ($('#meldung').innerHTML = '');
    try { await arbeit(); } catch (e) { fehlerZeigen(e); } finally { k.disabled = false; }
  });
}

// ── Protokoll eines Hintergrundauftrags ───────────────────────────────────────
function protokollVerfolgen(fertig) {
  let seit = 0;
  const pre = $('#protokoll');
  umfrage = setInterval(async () => {
    try {
      const a = await api(`/api/auftrag?seit=${seit}`);
      if (a.zeilen.length) {
        pre.textContent += a.zeilen.join('\n') + '\n';
        pre.scrollTop = pre.scrollHeight;
        seit = a.gesamt;
      }
      if (!a.laeuft) {
        clearInterval(umfrage); umfrage = null;
        if (a.fehler) fehlerZeigen(new Error(a.fehler));
        else fertig(a.ergebnis);
      }
    } catch (e) { /* kurz weg – beim nächsten Takt weiter */ }
  }, 1000);
}

// ── Schritte ──────────────────────────────────────────────────────────────────
function passwort() {
  $('#inhalt').innerHTML = `
    <h1>Passwort für diesen PC</h1>
    <p>Der PC meldet sich automatisch als <b>studio</b> an. Das Passwort braucht man für Wartung und Updates –
       bitte ein eigenes festlegen und gut aufbewahren.</p>
    <div class="raster">
      <label class="feld"><span>Neues Passwort</span><input type="password" id="pw1" autocomplete="new-password"></label>
      <label class="feld"><span>Wiederholen</span><input type="password" id="pw2" autocomplete="new-password"></label>
    </div>
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="weiter">Passwort setzen</button></div>`;
  knopf('#weiter', async () => {
    const a = $('#pw1').value, b = $('#pw2').value;
    if (a.length < 8) throw new Error('Mindestens 8 Zeichen.');
    if (a !== b) throw new Error('Die beiden Eingaben stimmen nicht überein.');
    await api('/api/passwort', { passwort: a });
    await neuLaden();
  });
}

async function internet() {
  $('#inhalt').innerHTML = `
    <h1>Internetverbindung</h1>
    <p>Für die Einrichtung braucht der PC Internet: Die Studio-Cloud prüft die Lizenz, und die Software kommt von GitHub.</p>
    <div id="pruefung"><p class="caption">Prüfe …</p></div>
    <div id="meldung"></div>
    <div class="zeile"><button id="nochmal">Erneut prüfen</button><button class="haupt" id="weiter" disabled>Weiter</button></div>`;
  const pruefen = async () => {
    $('#pruefung').innerHTML = '<p class="caption">Prüfe …</p>';
    const r = await api('/api/internet', {});
    internetOk = r.ok;
    const zeile = (ok, text) => `<p><span class="badge ${ok ? 'ok' : 'nein'}">${ok ? 'erreichbar' : 'nicht erreichbar'}</span> ${text}</p>`;
    $('#pruefung').innerHTML = zeile(r.cloud, 'Studio-Cloud') + zeile(r.registry, 'Software-Download (ghcr.io)') +
      (r.ok ? '' : '<div class="box warnung">Bitte Netzwerkkabel oder WLAN prüfen und erneut prüfen.</div>');
    $('#weiter').disabled = !r.ok;
    seitenleiste();
  };
  knopf('#nochmal', pruefen);
  knopf('#weiter', () => neuLaden());
  try { await pruefen(); } catch (e) { fehlerZeigen(e); }
}

function docker() {
  $('#inhalt').innerHTML = `
    <h1>Docker installieren</h1>
    <p>Studio läuft in Docker-Containern. Das Installieren dauert je nach Leitung ein paar Minuten.</p>
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="los">Docker installieren</button></div>
    <pre class="protokoll" id="protokoll"></pre>`;
  knopf('#los', async () => {
    await api('/api/docker', {});
    $('#los').disabled = true;
    protokollVerfolgen(() => neuLaden());
  });
}

function lizenz() {
  const a = stand.aktivierung;
  $('#inhalt').innerHTML = `
    <h1>Studio-Lizenz</h1>
    <p>Den Lizenzschlüssel bekommen Sie vom Plattform-Betreiber. Er beginnt mit <b>STUDIO-1.</b> – bitte einfügen
       oder aus einer Textdatei laden (z. B. vom USB-Stick).</p>
    ${a ? `<div class="box ok">Dieser PC ist aktiviert für <b>${esc(a.tenant_name)}</b>. Ein neuer Schlüssel ersetzt das.</div>` : ''}
    <label class="feld"><span>Lizenzschlüssel</span><textarea id="schluessel" spellcheck="false" placeholder="STUDIO-1.…"></textarea></label>
    <div class="zeile">
      <label class="knopf" for="datei">Aus Datei laden</label>
      <input type="file" id="datei" accept=".txt,.key,.lic,text/plain" hidden>
      <span class="caption" id="vorpruefung"></span>
    </div>
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="aktivieren">Aktivieren</button>
      ${a ? '<button id="weiter">Weiter ohne Änderung</button>' : ''}</div>`;
  const vorpruefen = async () => {
    const s = $('#schluessel').value.trim();
    if (!s) { $('#vorpruefung').textContent = ''; return; }
    try {
      const r = await api('/api/lizenz/pruefen', { schluessel: s });
      $('#vorpruefung').textContent = `Schlüssel echt${r.gueltig_bis ? `, gültig bis ${new Date(r.gueltig_bis).toLocaleDateString('de-DE')}` : ', unbefristet'}.`;
    } catch (e) { $('#vorpruefung').textContent = e.message; }
  };
  $('#schluessel').addEventListener('input', vorpruefen);
  $('#datei').addEventListener('change', async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    const text = await f.text();
    $('#schluessel').value = (text.match(/STUDIO-1\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+/) || [text.trim()])[0];
    await vorpruefen();
  });
  knopf('#aktivieren', async () => {
    const r = await api('/api/aktivieren', { schluessel: $('#schluessel').value });
    $('#inhalt').innerHTML = `
      <h1>Aktiviert</h1>
      <div class="box ok">Dieser PC wird Edge für <b>${esc(r.tenant_name)}</b>.</div>
      ${r.neuinstallation ? '<p class="caption">Derselbe PC war schon aktiviert – das ist eine Neuinstallation. Die bisherige Verbindung gilt nicht mehr.</p>' : ''}
      ${r.konfiguration_vorhanden ? '<p>Für dieses Studio ist eine Konfiguration hinterlegt – sie wird übernommen.</p>' : ''}
      <div class="zeile"><button class="haupt" id="weiter">Weiter</button></div>`;
    knopf('#weiter', () => neuLaden());
  });
  knopf('#weiter', () => neuLaden('fernwartung'));
}

function fernwartung() {
  const a = stand.aktivierung, f = stand.fernwartung;
  const wahlen = [
    { id: 'plattform', titel: 'Fernwartung durch die Plattform (empfohlen)', aus: !a.tailscale_verfuegbar,
      text: a.tailscale_verfuegbar ? 'Der Support erreicht diesen PC sicher über Tailscale – für Hilfe, Wartung und Updates.'
        : 'Die Plattform hat dafür keinen Schlüssel hinterlegt.' },
    { id: 'eigen', titel: 'Eigenes Tailscale', text: 'Mit einem Anmeldeschlüssel aus Ihrem eigenen Tailscale-Konto.' },
    { id: 'aus', titel: 'Ohne Fernwartung', text: 'Tailscale wird nicht installiert.' },
  ];
  const start = f?.wahl || (a.tailscale_verfuegbar ? 'plattform' : 'eigen');
  $('#inhalt').innerHTML = `
    <h1>Fernwartung</h1>
    <p>Über Tailscale kann der Support diesen PC aus der Ferne erreichen – verschlüsselt, ohne offene Ports im Studio-Netz.</p>
    <div class="wahl">${wahlen.map((w) => `
      <label class="${w.aus ? 'aus' : ''}" data-wahl="${w.id}">
        <input type="radio" name="wahl" value="${w.id}" ${w.aus ? 'disabled' : ''}>
        <span class="titel">${esc(w.titel)}</span><span class="text">${esc(w.text)}</span>
      </label>`).join('')}</div>
    <div id="eigen" hidden>
      <label class="feld"><span>Tailscale-Anmeldeschlüssel</span>
        <input type="password" id="tskey" placeholder="tskey-auth-…" autocomplete="off">
        <span class="hinweis">Tailscale-Admin-Konsole → Settings → Keys → „Generate auth key“.</span></label>
    </div>
    <div id="nicht" hidden>
      <div class="box warnung"><b>Eingeschränkter Service.</b> Tailscale wird nicht installiert. Fernhilfe, Fernwartung
        und Updates aus der Ferne sind dann nicht möglich – es gibt nur eingeschränkten Service.</div>
      <label class="feld"><span><input type="checkbox" id="verstanden"> Verstanden – ohne Fernwartung einrichten</span></label>
    </div>
    <label class="feld" id="namefeld"><span>Gerätename im Tailnet</span>
      <input type="text" id="name" value="${esc(f?.name || a.tailscale_hostname || 'studio-edge')}"></label>
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="weiter">Übernehmen</button></div>`;
  const setzen = (id) => {
    document.querySelectorAll('.wahl label').forEach((l) => l.classList.toggle('gewaehlt', l.dataset.wahl === id));
    const radio = $(`input[value="${id}"]`); if (radio) radio.checked = true;
    $('#eigen').hidden = id !== 'eigen';
    $('#nicht').hidden = id !== 'aus';
    $('#namefeld').hidden = id === 'aus';
  };
  document.querySelectorAll('input[name=wahl]').forEach((r) => r.addEventListener('change', () => setzen(r.value)));
  setzen(start);
  knopf('#weiter', async () => {
    const wahl = $('input[name=wahl]:checked')?.value;
    if (wahl === 'aus' && !$('#verstanden').checked) throw new Error('Bitte bestätigen, dass es ohne Fernwartung nur eingeschränkten Service gibt.');
    await api('/api/fernwartung', { wahl, schluessel: $('#tskey').value, name: $('#name').value });
    await neuLaden();
  });
}

async function konfiguration() {
  $('#inhalt').innerHTML = '<h1>Konfiguration</h1><p class="caption">Lade Vorschläge …</p><div id="meldung"></div>';
  let v;
  try { v = await api('/api/vorschlaege'); } catch (e) { fehlerZeigen(e); return; }
  if (v.cloud) {
    $('#inhalt').innerHTML = `
      <h1>Konfiguration</h1>
      <div class="box info">Für dieses Studio ist eine Konfiguration in der Cloud hinterlegt. Sie wird übernommen;
        Verbindung zur Cloud und Fernwartung setzt die Aktivierung neu.</div>
      <table class="werte">${Object.entries(v.cloud).map(([k, w]) => `<tr><td>${esc(k)}</td><td>${esc(w)}</td></tr>`).join('')}</table>
      <div id="meldung"></div>
      <div class="zeile"><button class="haupt" id="speichern">Übernehmen</button></div>`;
    knopf('#speichern', async () => { await api('/api/konfiguration', {}); await neuLaden(); });
    return;
  }
  const karten = v.karten.map((k) => `<option value="${esc(k.name)}" ${k.name === v.karte ? 'selected' : ''}>${esc(k.name)} · ${k.wlan ? 'WLAN' : 'Kabel'}${k.ip ? ' · ' + esc(k.ip) : ''}</option>`).join('');
  $('#inhalt').innerHTML = `
    <h1>Konfiguration</h1>
    <p>Für dieses Studio ist noch nichts hinterlegt. Die Werte werden als <code>.env</code> abgelegt und an den
       Plattform-Betreiber geschickt. Passwörter und Schlüssel erzeugt der Installer selbst.</p>
    <h2>Studio-Netz</h2>
    <div class="raster">
      <label class="feld"><span>Netzwerkkarte am Studio-Netz</span><select id="karte">${karten}</select>
        <span class="hinweis">Kabel ist besser: Geräte im Studio-Netz erreicht der PC darüber zuverlässiger.</span></label>
      <label class="feld"><span>Feste LAN-Adresse dieses PCs</span><input type="text" id="lan_ip" value="${esc(v.lan_ip)}">
        <span class="hinweis">Dorthin schicken die QR-Leser ihre Scans. Im Router als feste Adresse (DHCP-Reservierung) eintragen.</span></label>
    </div>
    <label class="feld"><span><input type="checkbox" id="geraetenetz"> Gerätenetz-Helfer einschalten</span>
      <span class="hinweis">Nur nötig für Geräte in einem fremden Adressbereich, etwa ein Tür-Relay in Werkseinstellung. Braucht Kabel.</span></label>
    <h2>Marke in der Oberfläche</h2>
    <div class="raster">
      <label class="feld"><span>Name</span><input type="text" id="marke" value="${esc(v.marke)}" maxlength="30"></label>
      <label class="feld"><span>Zusatz</span><input type="text" id="akzent" placeholder="z. B. No.1" maxlength="12"></label>
    </div>
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="speichern">Speichern und an die Cloud senden</button></div>`;
  knopf('#speichern', async () => {
    await api('/api/konfiguration', {
      karte: $('#karte').value, lan_ip: $('#lan_ip').value.trim(), geraetenetz: $('#geraetenetz').checked,
      marke: $('#marke').value, akzent: $('#akzent').value,
    });
    await neuLaden();
  });
}

function installation() {
  const laeuft = stand.auftrag?.laeuft && stand.auftrag?.name === 'Studio installieren';
  $('#inhalt').innerHTML = `
    <h1>Studio installieren</h1>
    <p>Die Software wird geladen, geprüft und gestartet. Beim ersten Start richtet Studio seine Datenbank ein –
       das dauert einige Minuten. Bitte den PC so lange eingeschaltet lassen.</p>
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="los" ${laeuft ? 'disabled' : ''}>Studio installieren</button></div>
    <pre class="protokoll" id="protokoll"></pre>`;
  const verfolgen = () => protokollVerfolgen(() => neuLaden('fertig'));
  if (laeuft) verfolgen();
  knopf('#los', async () => { await api('/api/installieren', {}); $('#los').disabled = true; verfolgen(); });
}

async function fertig() {
  const i = stand.installiert;
  $('#inhalt').innerHTML = '<h1>Studio ist eingerichtet</h1><p class="caption">Lade Anmeldedaten …</p>';
  let zugang = null;
  try { zugang = (await api('/api/anmeldedaten')).zugang; } catch { /* ohne Datei: schon angemeldet */ }
  const fern = i.fernwartung === 'aus'
    ? '<span class="badge nein">abgewählt – eingeschränkter Service</span>'
    : i.tailscale ? `<span class="badge ok">aktiv</span> ${esc(i.tailscale.name)} (${esc(i.tailscale.ip)})`
      : '<span class="badge nein">nicht angemeldet</span> – bitte den Support ansprechen';
  $('#inhalt').innerHTML = `
    <h1>Studio ist eingerichtet</h1>
    <table class="werte">
      <tr><td>Studio</td><td>${esc(i.studio)}</td></tr>
      <tr><td>Version</td><td>${esc(i.version)}</td></tr>
      <tr><td>Lizenz</td><td>${i.lizenz_aktiv ? '<span class="badge ok">aktiv</span>' : '<span class="badge nein">noch nicht aktiv</span> – Studio versucht es weiter'}</td></tr>
      <tr><td>Fernwartung</td><td>${fern}</td></tr>
    </table>
    ${zugang ? `
      <h2>Erste Anmeldung</h2>
      <div class="zugang"><span>Benutzer</span><code>${esc(zugang.benutzer)}</code><span>Startpasswort</span><code>${esc(zugang.passwort)}</code></div>
      <div class="box warnung">Bitte notieren. Beim ersten Anmelden muss das Passwort geändert werden.</div>` : ''}
    <div class="zeile"><a class="knopf haupt" href="http://localhost/" target="_blank" rel="noreferrer">Studio öffnen</a></div>
    <p class="caption">Ab jetzt öffnet das Studio-Icon auf dem Schreibtisch direkt die Kasse.</p>`;
}

neuLaden().catch((e) => { $('#inhalt').innerHTML = `<div class="box fehler">${esc(e.message)}</div>`; });
