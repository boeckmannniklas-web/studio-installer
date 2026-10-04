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
  { id: 'speicher', titel: 'Speicher & Sicherung' },
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
    case 'speicher': {
      const sp = stand.speicher || {};
      return !!(sp.daten?.festgelegt && sp.ziel && sp.schluessel && sp.wiederherstellung?.wahl);
    }
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
    li.addEventListener('click', () => {
      // Nach der Installation lässt sich nur noch das Backup-Ziel ändern (Schritt Speicher).
      if (!stand?.installiert || ['fertig', 'speicher'].includes(li.dataset.schritt)) zeigen(li.dataset.schritt);
    }));
}

function zeigen(id) {
  if (umfrage) { clearInterval(umfrage); umfrage = null; }
  aktuell = id;
  seitenleiste();
  ({ passwort, internet, docker, lizenz, fernwartung, konfiguration, speicher, installation, fertig }[id])();
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

function updatesBlock(u) {
  const modus = u?.modus || 'automatisch';
  return `
    <h2>Updates</h2>
    <p class="caption">Neue Studio-Versionen kommen erst, wenn der Plattform-Betreiber sie freigegeben hat. Vorher wird
      immer gesichert, und misslingt ein Update, geht es von selbst auf die alte Version zurück. Umstellen lässt sich das
      später im Studio unter Grundeinstellungen → Updates.</p>
    <div class="wahl">
      <label data-upd="automatisch"><input type="radio" name="updmodus" value="automatisch" ${modus === 'automatisch' ? 'checked' : ''}>
        <span class="titel">Automatisch im Wartungsfenster (empfohlen)</span>
        <span class="text">Studio spielt freigegebene Updates nachts selbst ein – nie während eines Verkaufs.</span></label>
      <label data-upd="manuell"><input type="radio" name="updmodus" value="manuell" ${modus === 'manuell' ? 'checked' : ''}>
        <span class="titel">Manuell</span>
        <span class="text">Der SystemOwner sucht und startet Updates selbst im Studio.</span></label>
    </div>
    <div class="raster">
      <label class="feld"><span>Wartungsfenster von</span><input type="time" id="fenster_von" value="${esc(u?.fenster_von || '03:00')}"></label>
      <label class="feld"><span>bis</span><input type="time" id="fenster_bis" value="${esc(u?.fenster_bis || '05:00')}">
        <span class="hinweis">In diesem Fenster laufen auch Sicherheitsupdates des Betriebssystems und ein nötiger Neustart.</span></label>
    </div>`;
}

function updatesEingaben() {
  return { modus: $('input[name=updmodus]:checked')?.value || 'automatisch',
           fenster_von: $('#fenster_von').value, fenster_bis: $('#fenster_bis').value };
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
      ${updatesBlock(stand.speicher?.updates)}
      <div id="meldung"></div>
      <div class="zeile"><button class="haupt" id="speichern">Übernehmen</button></div>`;
    knopf('#speichern', async () => { await api('/api/konfiguration', { updates: updatesEingaben() }); await neuLaden(); });
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
    ${updatesBlock(stand.speicher?.updates)}
    <div id="meldung"></div>
    <div class="zeile"><button class="haupt" id="speichern">Speichern und an die Cloud senden</button></div>`;
  knopf('#speichern', async () => {
    await api('/api/konfiguration', {
      karte: $('#karte').value, lan_ip: $('#lan_ip').value.trim(), geraetenetz: $('#geraetenetz').checked,
      marke: $('#marke').value, akzent: $('#akzent').value, updates: updatesEingaben(),
    });
    await neuLaden();
  });
}

// ── Speicher & Sicherung ──────────────────────────────────────────────────────
const gb = (b) => b == null ? '–' : b >= 1e12 ? `${(b / 1e12).toFixed(1)} TB` : `${Math.round(b / 1e9)} GB`;
const plattenName = (p) => `${esc(p.modell || p.name)} · ${gb(p.groesse)}${p.anschluss ? ' · ' + esc(p.anschluss.toUpperCase()) : ''} (${esc(p.name)})`;

function auftragAbwarten(fertig) {
  $('#protokoll').hidden = false;
  $('#protokoll').textContent = '';
  protokollVerfolgen(fertig);
}

async function speicher() {
  const sp = stand.speicher || {};
  const nachInstallation = !!stand.installiert;
  $('#inhalt').innerHTML = '<h1>Speicher & Sicherung</h1><p class="caption">Lade Platten …</p><div id="meldung"></div>';
  let pl = { platten: [], bestand: false };
  try { pl = await api('/api/platten'); } catch (e) { fehlerZeigen(e); }
  const frei = pl.platten.filter((p) => !p.gesperrt);
  const daten = sp.daten || {};
  const datenText = daten.art === 'platte' ? `Eigene Platte ${esc(daten.modell || '')} unter <code>${esc(daten.daten)}</code>`
    : daten.art === 'system' ? `Systemplatte unter <code>${esc(daten.daten)}</code>`
      : daten.art === 'bestand' ? 'Bestehende Installation – Daten bleiben in den Docker-Volumes' : '';

  $('#inhalt').innerHTML = `
    <h1>Speicher & Sicherung</h1>
    <p>Studio hält seine Daten auf einer Platte in diesem PC und sichert jede Nacht – vor Ort und verschlüsselt in die Cloud.</p>

    <h2>1 · Datenspeicher</h2>
    ${daten.festgelegt ? `<div class="box ok">${datenText}${daten.frei != null ? ` · ${gb(daten.frei)} frei` : ''}</div>` : pl.bestand ? `
      <div class="box info">Auf diesem PC liegen schon Studio-Daten aus einer früheren Installation. Sie bleiben, wo sie sind.</div>
      <div class="zeile"><button class="haupt" id="daten-los">Übernehmen</button></div>` : `
      <p class="caption">Hier liegt die laufende Datenbank. Eine Netzwerkfestplatte ist dafür nicht geeignet – bei jedem
        Netzaussetzer stünde die Kasse. Sie kann aber unten das Backup-Ziel sein.</p>
      <div class="wahl" id="daten-wahl">
        <label data-wahl="system"><input type="radio" name="daten" value="system" checked>
          <span class="titel">Systemplatte (Standard)</span><span class="text">Die Daten liegen auf der Platte, auf der auch Ubuntu läuft.</span></label>
        <label data-wahl="platte" class="${frei.length ? '' : 'aus'}"><input type="radio" name="daten" value="platte" ${frei.length ? '' : 'disabled'}>
          <span class="titel">Eigene Platte</span><span class="text">${frei.length ? 'Eine zweite interne Platte oder eine USB-Platte/SSD, nur für Studio.' : 'Keine weitere Platte gefunden – erst anschließen, dann Seite neu laden.'}</span></label>
      </div>
      <div id="daten-platte" hidden>
        <label class="feld"><span>Platte</span><select id="daten-geraet">${frei.map((p) => `<option value="${esc(p.name)}">${plattenName(p)}${p.studio_daten ? ' – Studio-Datenplatte' : ''}</option>`).join('')}</select></label>
        <div id="daten-hinweis"></div>
      </div>
      <div class="zeile"><button class="haupt" id="daten-los">Datenspeicher einrichten</button></div>`}

    <h2>2 · Backup-Ziel vor Ort</h2>
    ${sp.ziel ? `<div class="box ok">${zielText(sp.ziel)}</div><p class="caption">Ändern: unten neu wählen und übernehmen.</p>` : ''}
    <div class="wahl" id="ziel-wahl">
      ${[['spaeter', 'Später festlegen', 'Dann sichert Studio nur in die Cloud.'],
         ['usb', 'USB-Platte', 'Eine eigene Platte nur für Sicherungen – nicht die Datenplatte.'],
         ['smb', 'Netzwerkfestplatte (SMB)', 'Fritz!Box-Speicher, Synology, QNAP, Windows-Freigabe …'],
         ['nfs', 'Netzwerkfestplatte (NFS)', 'Für NAS mit NFS-Freigabe.']]
         .map(([id, t, x]) => `<label data-wahl="${id}"><input type="radio" name="ziel" value="${id}" ${(sp.ziel?.art || 'spaeter') === id ? 'checked' : ''}>
           <span class="titel">${t}</span><span class="text">${x}</span></label>`).join('')}
    </div>
    <div id="ziel-usb" hidden>
      <label class="feld"><span>Platte</span><select id="ziel-geraet">${frei.filter((p) => !p.studio_daten).map((p) => `<option value="${esc(p.name)}" data-fertig="${p.studio_backup ? '1' : ''}">${plattenName(p)}${p.studio_backup ? ' – Studio-Backup-Platte' : ''}</option>`).join('')}</select></label>
      <div id="ziel-usb-hinweis"></div>
    </div>
    <div id="ziel-netz" hidden>
      <div class="raster">
        <label class="feld"><span>Server</span><input type="text" id="ziel-server" placeholder="z. B. 192.168.178.5 oder nas" value="${esc(sp.ziel?.server || '')}"></label>
        <label class="feld" id="f-freigabe"><span>Freigabe</span><input type="text" id="ziel-freigabe" placeholder="z. B. studio-backup" value="${esc(sp.ziel?.freigabe || '')}"></label>
        <label class="feld" id="f-pfad"><span>Exportierter Pfad</span><input type="text" id="ziel-pfad" placeholder="/volume1/studio-backup" value="${esc(sp.ziel?.pfad || '')}"></label>
        <label class="feld" id="f-benutzer"><span>Benutzer</span><input type="text" id="ziel-benutzer" autocomplete="off" value="${esc(sp.ziel?.benutzer || '')}"></label>
        <label class="feld" id="f-passwort"><span>Passwort</span><input type="password" id="ziel-passwort" autocomplete="new-password"></label>
      </div>
    </div>
    <p class="caption">Unabhängig davon geht jede Sicherung verschlüsselt in die Cloud und bleibt dort 14 Tage.</p>
    <div class="zeile"><button id="ziel-testen">Testen</button><button class="haupt" id="ziel-los">Backup-Ziel übernehmen</button></div>

    <h2>3 · Sicherungsschlüssel</h2>
    ${sp.schluessel ? `<div class="box ok">Eingerichtet (Kennung <code>${esc(sp.fingerabdruck)}</code>). Sicherungen sind damit verschlüsselt –
        öffnen kann sie nur, wer das Wiederherstellungsblatt hat (oder die Plattform mit Ihrer Freigabe).</div>
      ${sp.blatt ? '<div class="zeile"><button id="blatt-zeigen">Wiederherstellungsblatt anzeigen</button></div>' : ''}`
    : nachInstallation ? '<div class="box warnung">Noch kein Schlüssel – im Studio unter Backup einrichten.</div>' : `
      <p class="caption">Jede Sicherung wird verschlüsselt. Den Schlüssel dazu bekommen Sie einmal als Wiederherstellungsblatt
        zum Ausdrucken; eine verschlüsselte Kopie bewahrt die Plattform auf. Hat Ihr Studio schon einen Schlüssel, gilt der weiter.</p>
      <div class="zeile"><button class="haupt" id="schluessel-los">Schlüssel einrichten</button></div>`}
    <div id="blatt"></div>

    ${nachInstallation ? '' : `
    <h2>4 · Neu beginnen oder wiederherstellen</h2>
    ${sp.wiederherstellung?.wahl === 'sicherung' ? `<div class="box ok">Sicherung vom ${esc(datumZeit(sp.wiederherstellung.manifest?.erstellt))}
        (Version ${esc(sp.wiederherstellung.manifest?.version)}) ist geladen und geprüft. Sie wird bei der Installation eingespielt.</div>`
      : sp.wiederherstellung?.wahl === 'neu' ? '<div class="box ok">Studio beginnt mit leeren Daten.</div>' : ''}
    <div class="wahl" id="wh-wahl">
      <label data-wahl="neu"><input type="radio" name="wh" value="neu" ${sp.wiederherstellung?.wahl !== 'sicherung' ? 'checked' : ''}>
        <span class="titel">Neu beginnen</span><span class="text">Für ein neues Studio oder einen zusätzlichen PC.</span></label>
      <label data-wahl="sicherung"><input type="radio" name="wh" value="sicherung" ${sp.wiederherstellung?.wahl === 'sicherung' ? 'checked' : ''}>
        <span class="titel">Aus einer Sicherung wiederherstellen</span><span class="text">Nach einem Defekt oder auf neuer Hardware.</span></label>
    </div>
    <div id="wh-sicherung" hidden><p class="caption">Lade Sicherungen …</p></div>
    <div class="zeile"><button class="haupt" id="wh-los">Übernehmen</button></div>`}

    <div id="meldung"></div>
    <pre class="protokoll" id="protokoll" hidden></pre>
    <div class="zeile"><button class="haupt" id="weiter" ${erledigt('speicher') && !nachInstallation ? '' : 'hidden'}>Weiter zur Installation</button></div>`;

  // Datenspeicher
  const datenSetzen = () => {
    const w = $('input[name=daten]:checked')?.value;
    document.querySelectorAll('#daten-wahl label').forEach((l) => l.classList.toggle('gewaehlt', l.dataset.wahl === w));
    if ($('#daten-platte')) $('#daten-platte').hidden = w !== 'platte';
    const p = frei.find((x) => x.name === $('#daten-geraet')?.value);
    if ($('#daten-hinweis')) $('#daten-hinweis').innerHTML = !p ? '' : p.studio_daten
      ? '<div class="box info">Auf dieser Platte liegt schon ein Studio-Datenspeicher. Er wird übernommen, nichts wird gelöscht.</div>'
      : `<div class="box warnung"><b>Alles auf dieser Platte wird gelöscht.</b>${p.wechselbar ? ' Eine USB-Platte darf im Betrieb nicht abgezogen werden.' : ''}
         <label class="feld"><span>Zum Bestätigen den Namen der Platte eintippen: <code>${esc(p.name)}</code></span><input type="text" id="daten-bestaetigung" autocomplete="off"></label></div>`;
  };
  document.querySelectorAll('input[name=daten]').forEach((r) => r.addEventListener('change', datenSetzen));
  $('#daten-geraet')?.addEventListener('change', datenSetzen);
  if ($('#daten-wahl')) datenSetzen();
  knopf('#daten-los', async () => {
    const art = pl.bestand ? 'bestand' : $('input[name=daten]:checked')?.value;
    await api('/api/speicher/daten', { art, platte: $('#daten-geraet')?.value, bestaetigung: $('#daten-bestaetigung')?.value || '' });
    auftragAbwarten(() => neuLaden('speicher'));
  });

  // Backup-Ziel
  const zielSetzen = () => {
    const w = $('input[name=ziel]:checked')?.value;
    document.querySelectorAll('#ziel-wahl label').forEach((l) => l.classList.toggle('gewaehlt', l.dataset.wahl === w));
    $('#ziel-usb').hidden = w !== 'usb';
    $('#ziel-netz').hidden = !['smb', 'nfs'].includes(w);
    ['#f-freigabe', '#f-benutzer', '#f-passwort'].forEach((f) => { $(f).hidden = w !== 'smb'; });
    $('#f-pfad').hidden = w !== 'nfs';
    $('#ziel-testen').hidden = w === 'spaeter';
    const opt = $('#ziel-geraet')?.selectedOptions[0];
    $('#ziel-usb-hinweis').innerHTML = !opt ? '<div class="box warnung">Keine freie Platte gefunden.</div>' : opt.dataset.fertig ? '' :
      `<div class="box warnung"><b>Die Platte wird gelöscht und für Sicherungen eingerichtet.</b>
        <label class="feld"><span>Zum Bestätigen den Namen eintippen: <code>${esc(opt.value)}</code></span><input type="text" id="ziel-bestaetigung" autocomplete="off"></label></div>`;
  };
  document.querySelectorAll('input[name=ziel]').forEach((r) => r.addEventListener('change', zielSetzen));
  $('#ziel-geraet')?.addEventListener('change', zielSetzen);
  zielSetzen();
  const zielEingaben = () => ({
    art: $('input[name=ziel]:checked')?.value, platte: $('#ziel-geraet')?.value, bestaetigung: $('#ziel-bestaetigung')?.value || '',
    server: $('#ziel-server').value.trim(), freigabe: $('#ziel-freigabe').value.trim(), pfad: $('#ziel-pfad').value.trim(),
    benutzer: $('#ziel-benutzer').value.trim(), passwort: $('#ziel-passwort').value,
  });
  knopf('#ziel-testen', async () => {
    await api('/api/speicher/ziel/testen', zielEingaben());
    auftragAbwarten((e) => {
      $('#meldung').innerHTML = e?.formatieren_noetig ? '<div class="box warnung">Die Platte muss erst eingerichtet werden – „Übernehmen“ formatiert sie.</div>'
        : `<div class="box ok">Test bestanden${e?.frei != null ? ` – ${gb(e.frei)} frei` : ''}.</div>`;
    });
  });
  knopf('#ziel-los', async () => {
    await api('/api/speicher/ziel', zielEingaben());
    auftragAbwarten(() => neuLaden('speicher'));
  });

  // Schlüssel
  knopf('#schluessel-los', async () => {
    const r = await api('/api/sicherung/schluessel', {});
    await neuLaden('speicher');
    if (r.neu) await blattZeigen();
    else $('#meldung').innerHTML = '<div class="box info">Ihr Studio hatte schon einen Sicherungsschlüssel – das Wiederherstellungsblatt von damals gilt weiter.</div>';
  });
  knopf('#blatt-zeigen', blattZeigen);

  // Neu oder Wiederherstellen
  let liste = null;
  const whSetzen = async () => {
    const w = $('input[name=wh]:checked')?.value;
    document.querySelectorAll('#wh-wahl label').forEach((l) => l.classList.toggle('gewaehlt', l.dataset.wahl === w));
    $('#wh-sicherung').hidden = w !== 'sicherung';
    if (w === 'sicherung' && !liste) {
      try { liste = await api('/api/sicherungen'); } catch (e) { $('#wh-sicherung').innerHTML = `<div class="box fehler">${esc(e.message)}</div>`; return; }
      const alle = [...liste.cloud.map((s) => ({ ...s, wert: `cloud:${s.id}`, wo: 'Cloud' })),
                    ...liste.vor_ort.map((s) => ({ ...s, wert: `ziel:${s.id}`, wo: 'Backup-Ziel' }))];
      $('#wh-sicherung').innerHTML = alle.length ? `
        <label class="feld"><span>Sicherung</span><select id="wh-datei">${alle.map((s) => `<option value="${esc(s.wert)}">${esc(datumZeit(s.erstellt))} · ${esc(s.wo)} · ${gb(s.groesse)}${s.anlass ? ' · ' + esc(s.anlass) : ''}</option>`).join('')}</select></label>
        <label class="feld"><span>Schlüssel vom Wiederherstellungsblatt</span><textarea id="wh-schluessel" spellcheck="false" placeholder="AGE-SECRET-KEY-1…"></textarea></label>
        <label class="feld"><span><input type="checkbox" id="wh-plattform" ${liste.freigabe_aktiv ? '' : 'disabled'}> Schlüssel bei der Plattform anfordern</span>
          <span class="hinweis">${liste.freigabe_aktiv ? 'Die Plattform hat die Herausgabe freigegeben.' : 'Geht erst, wenn der Plattform-Betreiber die Herausgabe freigegeben hat (Blatt verloren).'}</span></label>`
        : '<div class="box warnung">Für dieses Studio gibt es keine Sicherung – weder in der Cloud noch am Backup-Ziel.</div>';
    }
  };
  document.querySelectorAll('input[name=wh]').forEach((r) => r.addEventListener('change', whSetzen));
  if ($('#wh-wahl')) await whSetzen();
  knopf('#wh-los', async () => {
    if ($('input[name=wh]:checked')?.value === 'neu') { await api('/api/wiederherstellung/neu', {}); await neuLaden('speicher'); return; }
    const [quelle, ...rest] = ($('#wh-datei')?.value || '').split(':');
    if (!quelle) throw new Error('Keine Sicherung gewählt.');
    await api('/api/wiederherstellung/laden', { quelle, id: rest.join(':'), schluessel: $('#wh-schluessel').value, plattform: $('#wh-plattform').checked });
    auftragAbwarten(() => neuLaden('speicher'));
  });
  knopf('#weiter', () => neuLaden());
}

function zielText(z) {
  if (z.art === 'usb') return `USB-Platte ${esc(z.modell || z.platte || '')}`;
  if (z.art === 'smb') return `Netzwerkfestplatte <code>//${esc(z.server)}/${esc(z.freigabe)}</code> (SMB, Benutzer ${esc(z.benutzer)})`;
  if (z.art === 'nfs') return `Netzwerkfestplatte <code>${esc(z.server)}:${esc(z.pfad)}</code> (NFS)`;
  return 'Kein Ziel vor Ort – Sicherungen gehen nur in die Cloud.';
}

function datumZeit(iso) { return iso ? new Date(iso).toLocaleString('de-DE', { dateStyle: 'medium', timeStyle: 'short' }) : '–'; }

async function blattZeigen() {
  const { blatt } = await api('/api/sicherung/blatt');
  if (!blatt) { $('#blatt').innerHTML = '<div class="box warnung">Das Blatt ist nicht mehr verfügbar.</div>'; return; }
  const gruppen = blatt.privat.match(/.{1,6}/g).join(' ');
  $('#blatt').innerHTML = `
    <div class="blatt" id="blatt-druck">
      <div class="blatt-kopf"><b>Studio · Wiederherstellungsblatt</b><span>${esc(blatt.studio)} · erstellt am ${esc(blatt.erstellt)}</span></div>
      <p>Mit diesem Schlüssel lassen sich die Sicherungen dieses Studios öffnen – nach einem Defekt oder auf neuer Hardware.
         <b>Sicher aufbewahren</b> (Tresor, Ordner beim Steuerberater). Wer das Blatt hat, kann die Sicherungen lesen.</p>
      <div class="blatt-schluessel"><code>${esc(gruppen)}</code>${blatt.qr_svg ? `<div class="blatt-qr">${blatt.qr_svg}</div>` : ''}</div>
      <table class="werte"><tr><td>Kennung</td><td>${esc(blatt.fingerabdruck)}</td></tr>
        <tr><td>Öffentlicher Schlüssel</td><td><code>${esc(blatt.oeffentlich)}</code></td></tr></table>
      <p class="caption">Wiederherstellen: Studio-Einrichtung → Speicher & Sicherung → „Aus einer Sicherung wiederherstellen“, Schlüssel eintippen
         (Leerzeichen sind egal). Notfalls von Hand: <code>age -d -i schluessel.txt sicherung.tar.gz.age | tar -xz</code></p>
    </div>
    <div class="zeile"><button class="blau" id="drucken">Drucken</button>
      <span class="caption">Das Blatt bleibt bis zum Ende der Einrichtung abrufbar, danach nicht mehr.</span></div>`;
  $('#drucken').addEventListener('click', () => window.print());
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
    ${i.wiederhergestellt ? `<div class="box warnung"><b>Wiederhergestellt aus der Sicherung vom ${esc(datumZeit(i.wiederhergestellt))}.</b>
      Die Kasse bleibt gesperrt, bis der SystemOwner im Studio unter Backup den Abgleich mit Cloud und TSE durchgeführt hat –
      so fehlt kein Verkauf und keine Bon-Nummer wird doppelt vergeben.</div>` : ''}
    ${zugang ? `
      <h2>Erste Anmeldung</h2>
      <div class="zugang"><span>Benutzer</span><code>${esc(zugang.benutzer)}</code><span>Startpasswort</span><code>${esc(zugang.passwort)}</code></div>
      <div class="box warnung">Bitte notieren. Beim ersten Anmelden muss das Passwort geändert werden.</div>` : ''}
    <div class="zeile"><a class="knopf haupt" href="http://localhost/" rel="noreferrer">Studio öffnen</a></div>
    <p class="caption">Ab jetzt startet der PC direkt mit der Kasse im Vollbild. Alt+F4 schließt das Vollbild,
      das Studio-Icon auf dem Schreibtisch öffnet es wieder.</p>`;
}

neuLaden().catch((e) => { $('#inhalt').innerHTML = `<div class="box fehler">${esc(e.message)}</div>`; });
