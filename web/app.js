"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const STROM = { start: "Starten", herunterfahren: "Herunterfahren", neustart: "Neustart", stopp: "Hart ausschalten" };
  const WAS = { alles: "System und App", system: "Betriebssystem", app: "App" };
  const STATUS = { ok: "erfolgreich", warnung: "mit Warnung", fehler: "fehlgeschlagen", laeuft: "läuft", wartet: "wartet" };
  const ZEICHEN = { ok: "✔", warnung: "⚠", fehler: "✘", laeuft: "⟳", wartet: "…" };
  const RANG = { lesen: 0, bedienen: 1, admin: 2 };

  let stand = null;
  let timer = null;
  let aufgeschoben = false;
  let tokenSichtbar = false;
  let protokollTimer = null;

  // --- Hilfen --------------------------------------------------------------------------------------
  function h(tag, attrs, ...kinder) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kind of kinder.flat(3)) {
      if (kind === null || kind === undefined || kind === false || kind === "") continue;
      el.append(kind instanceof Node ? kind : document.createTextNode(String(kind)));
    }
    return el;
  }
  const zahlFormat = new Intl.NumberFormat("de-DE", { maximumFractionDigits: 1 });
  function bytes(n) {
    if (!n) return "0 B";
    const einheiten = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < einheiten.length - 1) { n /= 1024; i++; }
    return `${zahlFormat.format(n)} ${einheiten[i]}`;
  }
  const prozent = (a) => `${Math.round((a || 0) * 100)} %`;
  function dauer(s) {
    s = Math.floor(s || 0);
    const t = Math.floor(s / 86400), std = Math.floor((s % 86400) / 3600), min = Math.floor((s % 3600) / 60);
    if (t) return `${t} T ${std} Std`;
    if (std) return `${std} Std ${min} Min`;
    return s < 60 ? `${s} s` : `${min} Min`;
  }
  function zeit(ts) {
    if (!ts) return "–";
    const d = new Date(ts * 1000);
    const tag = (versatz) => new Date(Date.now() + versatz * 86400000).toDateString();
    const uhr = d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === tag(0)) return `heute ${uhr}`;
    if (d.toDateString() === tag(-1)) return `gestern ${uhr}`;
    if (d.toDateString() === tag(1)) return `morgen ${uhr}`;
    return `${d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" })} ${uhr}`;
  }
  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("zeigen");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove("zeigen"), 4000);
  }
  async function api(pfad, daten) {
    const opt = daten === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-ECSPVE": "1" },
      body: JSON.stringify(daten),
    };
    const r = await fetch(pfad, { credentials: "same-origin", ...opt });
    let json = null;
    try { json = await r.json(); } catch (e) { /* keine JSON-Antwort */ }
    if (r.status === 401 && !pfad.startsWith("/api/anmelden") && !pfad.startsWith("/api/zwei-faktor/")) {
      zeigeAnmeldung();
      throw new Error("Bitte anmelden.");
    }
    if (!r.ok) {
      const fehler = new Error((json && json.fehler) || `Fehler ${r.status}`);
      fehler.daten = json || {};
      throw fehler;
    }
    return json;
  }
  // Was die eigene Rolle erlaubt – der Server prüft es ohnehin, hier geht es nur ums Anzeigen
  const darf = (rolle) => Boolean(stand && stand.ich) && RANG[stand.ich.rolle] >= RANG[rolle];
  function gastName(id) {
    const g = stand && stand.gaeste.find((x) => x.id === id);
    return g ? g.name : `#${id}`;
  }
  function beschreibung(a) {
    const p = a.param || {};
    const ziel = a.gast !== null && a.gast !== undefined ? ` · ${gastName(a.gast)}` : "";
    if (a.art === "strom") return `${STROM[p.aktion] || p.aktion}${ziel}`;
    if (a.art === "update") return `Update ${WAS[p.was] || ""}${ziel}`;
    if (a.art === "dienst") return `${p.dienst} neu starten${ziel}`;
    if (a.art === "host-update") return "Proxmox-Host aktualisieren";
    if (a.art === "auffrischen") return "Paketlisten neu laden";
    if (a.art === "anlegen") return `Container „${p.name}“ anlegen${a.gast ? ` (${a.gast})` : ""}`;
    return a.titel || a.art;
  }

  // --- Laden ---------------------------------------------------------------------------------------
  async function laden() {
    clearTimeout(timer);
    try {
      stand = await api("/api/status");
    } catch (e) {
      if (!$("anmeldung").hidden) return;
      toast(e.message);
      timer = setTimeout(laden, 15000);
      return;
    }
    zeigeInhalt();
    zeichne();
    const laeuft = stand.auftraege.some((a) => a.status === "laeuft" || a.status === "wartet");
    timer = setTimeout(laden, laeuft ? 4000 : 10000);
  }
  function anmeldeSchritt(name) {
    for (const [id, schritt] of [["anmeldeform", "passwort"], ["neupwform", "neues-passwort"], ["codeform", "code"],
      ["einrichten", "einrichten"], ["notfall", "notfall"]]) {
      $(id).hidden = schritt !== name;
    }
    $("anmeldefehler").textContent = "";
    const fokus = { passwort: "benutzername", "neues-passwort": "neupw1", code: "code", einrichten: "einrichten-code" }[name];
    if (fokus) { $(fokus).value = ""; $(fokus).focus(); }
    if (name === "neues-passwort") $("neupw2").value = "";
  }
  function weiterNach(schritt) {
    if (schritt === "einrichten") einrichtenStarten();
    else anmeldeSchritt(schritt);
  }
  async function zeigeAnmeldung() {
    clearTimeout(timer);
    for (const d of document.querySelectorAll("dialog[open]")) d.close();
    $("inhalt").hidden = true;
    $("gesamt").hidden = true;
    $("abmelden").hidden = true;
    $("wer").hidden = true;
    $("anmeldung").hidden = false;
    // Nach dem Neuladen im richtigen Schritt weitermachen
    let s = { stufe: null };
    try { s = await api("/api/sitzung"); } catch (e) { /* dann eben beim Passwort */ }
    if (s.stufe === "passwort" && s.schritt) weiterNach(s.schritt);
    else anmeldeSchritt("passwort");
  }
  function qrBlock(d) {
    return [d.qr ? h("div", { class: "qr" }, h("img", { src: d.qr, alt: "QR-Code für die Authenticator-App", width: 220, height: 220 })) : null,
      h("p", { class: "leise-text" }, d.qr ? "Kein Scannen möglich? Schlüssel von Hand eingeben: " : "Schlüssel in der App von Hand eingeben: ",
        h("code", { class: "schluessel" }, d.geheim), " · ", h("a", { href: d.uri }, "auf diesem Handy direkt in der App öffnen"))];
  }
  async function einrichtenStarten() {
    try {
      const d = await api("/api/zwei-faktor/start", {});
      anmeldeSchritt("einrichten");
      $("einrichten-qr").hidden = !d.qr;
      if (d.qr) $("einrichten-qr").src = d.qr;
      $("einrichten-geheim").textContent = d.geheim;
      $("einrichten-link").href = d.uri;
    } catch (e) {
      anmeldeSchritt("passwort");
      $("anmeldefehler").textContent = e.message;
    }
  }
  function notfallZeigen(codes) {
    anmeldeSchritt("notfall");
    $("notfall-liste").replaceChildren(...codes.map((c) => h("li", {}, c)));
    $("notfall-kopieren").onclick = () => kopieren(codes.join("\n"));
  }
  async function kopieren(text) {
    try { await navigator.clipboard.writeText(text); toast("Kopiert"); }
    catch (e) { toast("Kopieren nicht möglich – bitte markieren und kopieren"); }
  }
  function zeigeInhalt() {
    $("anmeldung").hidden = true;
    $("inhalt").hidden = false;
    $("abmelden").hidden = false;
    $("gesamt").hidden = false;
    if (stand && stand.ich) {
      $("wer").hidden = false;
      $("wer").textContent = `${stand.ich.name} · ${stand.ich.rollen[stand.ich.rolle] || stand.ich.rolle}`;
    }
  }

  // --- Zeichnen ------------------------------------------------------------------------------------
  function zeichne() {
    // Ein offenes Menü nicht unter der Maus wegzeichnen – nach dem Schließen nachholen
    if (document.querySelector("details.menue[open]")) { aufgeschoben = true; return; }
    aufgeschoben = false;
    const host = stand.host || {};
    const uhr = new Date((stand.stand || stand.zeit) * 1000).toLocaleTimeString("de-DE");
    $("untertitel").textContent = host.name
      ? `${host.name} · Proxmox VE ${host.pve || "?"} · Stand ${uhr}`
      : "Übersicht der Container und Dienste";
    const g = $("gesamt");
    if (!stand.verbunden) { g.className = "pille fehler"; g.textContent = "Keine Verbindung"; }
    else if (stand.ok) { g.className = "pille ok"; g.textContent = "Alles in Ordnung"; }
    else { g.className = "pille fehler"; g.textContent = `${stand.probleme.length} ${stand.probleme.length === 1 ? "Problem" : "Probleme"}`; }
    $("pruefstand").textContent = stand.proben_stand ? `Dienste geprüft ${zeit(stand.proben_stand)}` : "Dienste werden geprüft …";
    meldungen();
    hostKarte(host);
    $("gaeste").replaceChildren(...(stand.gaeste.length ? stand.gaeste.map(gastKarte)
      : [h("p", { class: "leer" }, stand.verbunden ? "Keine Container oder VMs gefunden." : "Noch keine Daten vom Proxmox-Host.")]));
    auftragsListe();
    nachtKarte();
    schnittstellen();
    sicherheitKarte();
    benutzerKarte();
    $("alle").hidden = !darf("bedienen");
    $("anlegen").hidden = !darf("bedienen");
  }

  function meldungen() {
    const el = $("meldungen");
    el.replaceChildren();
    if (stand.probleme.length) {
      el.append(h("div", { class: "meldung fehler" },
        h("h3", {}, "Probleme"), h("ul", {}, stand.probleme.map((p) => h("li", {}, p.text)))));
    }
    const hinweise = [...stand.hinweise];
    const s = stand.sicherheit;
    if (s && !s.nur_proxy) hinweise.push("Sicherheit: Die Seite ist nicht nur über den Reverse Proxy erreichbar – PROXY_IP setzen (Karte „Sicherheit“).");
    if (s && s.zwei_faktor_seit && s.notfall_uebrig <= 2) hinweise.push(`Sicherheit: nur noch ${s.notfall_uebrig} Notfallcodes übrig – neue erzeugen.`);
    if (hinweise.length) {
      el.append(h("div", { class: "meldung hinweis" },
        h("h3", {}, "Hinweise"), h("ul", {}, hinweise.map((t) => h("li", {}, t)))));
    }
  }

  function sicherheitKarte() {
    const s = stand.sicherheit;
    if (!s) return;
    const datum = (ts) => new Date(ts * 1000).toLocaleString("de-DE", { dateStyle: "medium", timeStyle: "short" });
    const zeile = (titel, ok, text) => [h("dt", {}, titel), h("dd", { class: ok ? "gut" : "mittel" }, `${ok ? "✔" : "⚠"} ${text}`)];
    $("sicherheit").replaceChildren(
      h("dl", { class: "sicher-zeilen" },
        zeile("Zweiter Faktor", Boolean(s.zwei_faktor_seit), s.zwei_faktor_seit ? `eingerichtet seit ${datum(s.zwei_faktor_seit)}` : "nicht eingerichtet"),
        zeile("Notfallcodes", s.notfall_uebrig > 2, `${s.notfall_uebrig} übrig`),
        zeile("Zugang", s.nur_proxy, s.nur_proxy ? "nur über den Reverse Proxy"
          : "auch direkt erreichbar – PROXY_IP in /etc/ecspve/ecspve.env auf die IP eures Caddy setzen"),
        zeile("Verbindung", s.https, s.https ? "HTTPS" : "ohne HTTPS – bitte über die https-Adresse des Proxys öffnen"),
        h("dt", {}, "Angemeldet"), h("dd", {}, `${s.sitzungen} ${s.sitzungen === 1 ? "Sitzung (diese)" : "Sitzungen"}`)),
      h("div", { class: "werkzeuge" },
        h("button", { type: "button", onclick: () => zfOeffnen("geraet") }, "Weiteres Handy hinzufügen"),
        h("button", { type: "button", onclick: () => zfOeffnen("notfallcodes") }, "Neue Notfallcodes"),
        h("button", { type: "button", onclick: () => zfOeffnen("neu") }, "Neues Handy einrichten"),
        h("button", { type: "button", onclick: kontoOeffnen }, "Passwort ändern"),
        h("button", { type: "button", disabled: s.sitzungen < 2, onclick: andereAbmelden }, "Alle anderen abmelden")),
      h("h3", { class: "abstand" }, "Letzte Ereignisse"),
      s.ereignisse.length ? h("ul", { class: "ereignisse" }, s.ereignisse.map((e) =>
        h("li", { class: e.ok ? "" : "schlecht" }, h("span", { class: "leise-text" }, zeit(e.zeit)),
          h("span", { class: "mono" }, e.ip), h("span", {}, e.wer ? h("strong", {}, `${e.wer}: `) : null, e.text))))
        : h("p", { class: "leer" }, "Noch keine."));
  }
  async function andereAbmelden() {
    if (!confirm("Alle anderen angemeldeten Browser abmelden?")) return;
    try { await api("/api/sitzungen/beenden", {}); toast("Alle anderen Sitzungen sind abgemeldet"); laden(); }
    catch (e) { toast(e.message); }
  }

  // --- Benutzer (nur Admins) -----------------------------------------------------------------------
  function benutzerKarte() {
    const karte = $("benutzer-karte");
    karte.hidden = !stand.benutzer;
    if (!stand.benutzer) return;
    // Nicht unter der Hand neu zeichnen, während jemand in der Tabelle etwas auswählt
    if ($("benutzer").contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
    const datum = (ts) => (ts ? zeit(ts) : "nie");
    const zeilen = stand.benutzer.map((b) => {
      const ich = b.name === stand.ich.name;
      const rolle = h("select", { "aria-label": `Rolle von ${b.name}`, disabled: ich },
        Object.entries(stand.ich.rollen).map(([k, v]) => h("option", { value: k, selected: k === b.rolle }, v)));
      rolle.addEventListener("change", () => adminAktion("/api/benutzer/aendern", { name: b.name, rolle: rolle.value },
        `${b.name}: ${stand.ich.rollen[rolle.value]}`));
      const status = [b.aktiv ? null : h("span", { class: "marke-klein gesperrt" }, "gesperrt"),
        ich ? h("span", { class: "marke-klein ich" }, "du") : null,
        b.muss_aendern ? h("span", { class: "marke-klein" }, "Startpasswort") : null];
      const mehr = ich ? null : menue("…", [
        ["Passwort zurücksetzen", () => adminAktion("/api/benutzer/passwort", { name: b.name }, null,
          `Neues Startpasswort für „${b.name}“? Alle Sitzungen von „${b.name}“ werden abgemeldet.`)],
        ["Zweiten Faktor zurücksetzen", () => adminAktion("/api/benutzer/zwei-faktor", { name: b.name },
          `${b.name} richtet bei der nächsten Anmeldung eine neue App ein`,
          `Zweiten Faktor von „${b.name}“ zurücksetzen (z. B. Handy verloren)? „${b.name}“ wird abgemeldet und richtet bei der nächsten Anmeldung eine neue App ein.`)],
        [b.aktiv ? "Sperren" : "Entsperren", () => adminAktion("/api/benutzer/aendern", { name: b.name, aktiv: !b.aktiv },
          `${b.name} ${b.aktiv ? "gesperrt" : "entsperrt"}`, b.aktiv ? `„${b.name}“ sperren? Alle Sitzungen werden sofort beendet.` : null)],
        ["Löschen", () => adminAktion("/api/benutzer/loeschen", { name: b.name }, `${b.name} gelöscht`,
          `Benutzer „${b.name}“ endgültig löschen?`)],
      ]);
      return h("tr", {}, h("td", {}, h("strong", {}, b.name), " ", status), h("td", {}, rolle),
        h("td", { class: b.zwei_faktor ? "gut" : "mittel" }, b.zwei_faktor ? `✔ (${b.notfall_uebrig} Notfallcodes)` : "noch nicht eingerichtet"),
        h("td", { class: "leise-text" }, datum(b.letzte_anmeldung)), h("td", {}, mehr));
    });
    $("benutzer").replaceChildren(h("div", { class: "tabelle-rahmen" }, h("table", { class: "benutzer" },
      h("thead", {}, h("tr", {}, ["Benutzer", "Rolle", "Zweiter Faktor", "Zuletzt angemeldet", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, zeilen))),
    h("p", { class: "leise-text abstand" }, "Admin: alles, dazu Benutzer verwalten · Bedienen: Neustarts, Updates, Container anlegen · Nur ansehen: keine Aktionen. Neue Benutzer ersetzen ihr Startpasswort bei der ersten Anmeldung und richten ihren eigenen zweiten Faktor ein."));
  }

  // Benutzer verwalten braucht einen frisch bestätigten Code – fehlt er, einmal fragen und dann wiederholen
  function codeFreigabe() {
    return new Promise((fertig) => {
      const d = $("freigabe-dialog");
      const f = $("freigabe-form");
      f.elements.code.value = "";
      $("freigabe-fehler").textContent = "";
      f.onsubmit = async (ev) => {
        ev.preventDefault();
        try {
          await api("/api/benutzer/freigabe", { code: f.elements.code.value });
          d.onclose = null;
          d.close();
          fertig(true);
        } catch (e) { $("freigabe-fehler").textContent = e.message; f.elements.code.select(); }
      };
      d.onclose = () => fertig(false);
      d.showModal();
      f.elements.code.focus();
    });
  }
  async function mitFreigabe(pfad, daten) {
    try {
      return await api(pfad, daten);
    } catch (e) {
      if (!e.daten || !e.daten.code_noetig) throw e;
      if (!(await codeFreigabe())) return null;
      return api(pfad, daten);
    }
  }
  async function adminAktion(pfad, daten, meldung, frage) {
    if (frage && !confirm(frage)) { laden(); return; }
    try {
      const erg = await mitFreigabe(pfad, daten);
      if (erg && erg.passwort) startpasswortZeigen(erg.name, erg.passwort, "Neues Startpasswort");
      else if (erg && meldung) toast(meldung);
    } catch (e) { toast(e.message); }
    laden();
  }
  function startpasswortZeigen(name, passwort, titel) {
    $("benutzer-titel").textContent = titel;
    $("benutzer-eingabe").hidden = true;
    $("benutzer-knopf").hidden = true;
    $("benutzer-fehler").textContent = "";
    $("benutzer-ergebnis").replaceChildren(h("div", { class: "startpasswort" },
      h("p", {}, "Benutzer: ", h("strong", {}, name)),
      h("p", {}, "Startpasswort: ", h("code", {}, passwort)),
      h("p", { class: "leise-text" }, "Bitte sicher weitergeben (nicht per Mail). Es wird nur jetzt angezeigt. Bei der ersten Anmeldung wird es durch ein eigenes ersetzt, danach kommt der zweite Faktor."),
      h("p", {}, h("button", { type: "button", onclick: () => kopieren(passwort) }, "Kopieren"))));
    if (!$("benutzer-dialog").open) $("benutzer-dialog").showModal();
  }
  function benutzerNeuOeffnen() {
    const f = $("benutzer-form");
    f.reset();
    $("benutzer-titel").textContent = "Benutzer anlegen";
    $("benutzer-eingabe").hidden = false;
    $("benutzer-knopf").hidden = false;
    $("benutzer-ergebnis").replaceChildren();
    $("benutzer-fehler").textContent = "";
    $("benutzer-dialog").showModal();
    f.elements.name.focus();
  }
  async function benutzerNeuAbschicken(ev) {
    ev.preventDefault();
    const f = $("benutzer-form");
    $("benutzer-fehler").textContent = "";
    try {
      const erg = await mitFreigabe("/api/benutzer/anlegen", { name: f.elements.name.value.trim().toLowerCase(), rolle: f.elements.rolle.value });
      if (erg) startpasswortZeigen(erg.name, erg.passwort, "Benutzer angelegt");
      laden();
    } catch (e) { $("benutzer-fehler").textContent = e.message; }
  }

  // --- Eigenes Passwort ------------------------------------------------------------------------------
  function kontoOeffnen() {
    const f = $("konto-form");
    f.reset();
    $("konto-fehler").textContent = "";
    $("konto-dialog").showModal();
    f.elements.alt.focus();
  }
  async function kontoAbschicken(ev) {
    ev.preventDefault();
    const f = $("konto-form");
    if (f.elements.neu.value !== f.elements.neu2.value) { $("konto-fehler").textContent = "Die neuen Passwörter stimmen nicht überein."; return; }
    try {
      await api("/api/konto/passwort", { alt: f.elements.alt.value, neu: f.elements.neu.value });
      $("konto-dialog").close();
      toast("Passwort geändert – alle anderen Sitzungen sind abgemeldet");
      laden();
    } catch (e) { $("konto-fehler").textContent = e.message; }
  }

  // --- Zweiter Faktor verwalten ----------------------------------------------------------------------
  const ZF = {
    geraet: ["Weiteres Handy hinzufügen", "Zeigt den QR-Code noch einmal, damit ein weiteres Handy dieselben Codes bekommt. Dafür einmal den aktuellen Code eingeben."],
    notfallcodes: ["Neue Notfallcodes", "Erzeugt neue Notfallcodes – die bisherigen gelten danach nicht mehr."],
    neu: ["Neues Handy einrichten", "Für ein neues Handy oder nach einem Verlust: Danach gilt nur noch die neu eingerichtete App, alle anderen Handys und Sitzungen werden abgemeldet. Zum Bestätigen den aktuellen Code oder einen Notfallcode eingeben."],
  };
  let zfArt = null;
  let zfPhase = "code";
  function zfOeffnen(art) {
    zfArt = art;
    zfPhase = "code";
    const f = $("zf-form");
    $("zf-titel").textContent = ZF[art][0];
    $("zf-text").textContent = ZF[art][1];
    $("zf-label").firstChild.textContent = "Aktueller Code aus der App (oder ein Notfallcode) ";
    $("zf-eingabe").hidden = false;
    $("zf-knopf").hidden = false;
    $("zf-ergebnis").replaceChildren();
    $("zf-fehler").textContent = "";
    f.elements.code.value = "";
    $("zf-dialog").showModal();
    f.elements.code.focus();
  }
  async function zfAbschicken(ev) {
    ev.preventDefault();
    const f = $("zf-form");
    const code = f.elements.code.value.trim();
    const fertig = (inhalt) => { $("zf-eingabe").hidden = true; $("zf-knopf").hidden = true; $("zf-ergebnis").replaceChildren(...inhalt); };
    const codeListe = (codes) => [h("p", {}, "Jetzt ausdrucken oder im Passwort-Manager speichern – sie werden nicht noch einmal angezeigt:"),
      h("ul", { class: "codes" }, codes.map((c) => h("li", {}, c))),
      h("button", { type: "button", onclick: () => kopieren(codes.join("\n")) }, "Kopieren")];
    $("zf-fehler").textContent = "";
    try {
      if (zfArt === "geraet") {
        fertig(qrBlock(await api("/api/zwei-faktor/geraet", { code })));
      } else if (zfArt === "notfallcodes") {
        fertig(codeListe((await api("/api/zwei-faktor/notfallcodes", { code })).notfallcodes));
        laden();
      } else if (zfPhase === "code") {
        $("zf-ergebnis").replaceChildren(...qrBlock(await api("/api/zwei-faktor/start", { code })));
        $("zf-label").firstChild.textContent = "Code aus der NEUEN App ";
        $("zf-text").textContent = "Mit dem neuen Handy diesen QR-Code scannen und den Code daraus eingeben.";
        f.elements.code.value = "";
        f.elements.code.focus();
        zfPhase = "bestaetigen";
      } else {
        const d = await api("/api/zwei-faktor/bestaetigen", { code });
        $("zf-text").textContent = "Fertig – ab jetzt gilt nur noch die neue App.";
        fertig(codeListe(d.notfallcodes));
        laden();
      }
    } catch (e) { $("zf-fehler").textContent = e.message; f.elements.code.select(); }
  }

  function messwert(name, anteil, text) {
    const a = Math.max(0, Math.min(1, anteil || 0));
    const fuellung = h("div");
    fuellung.style.width = `${a * 100}%`;
    return h("div", { class: "messwert" },
      h("div", { class: "zeile" }, h("span", {}, name), h("span", {}, text)),
      h("div", { class: `balken${a > 0.9 ? " voll" : a > 0.75 ? " hoch" : ""}`, role: "img", "aria-label": `${name} ${prozent(a)}` }, fuellung));
  }

  function hostKarte(host) {
    const el = $("host");
    el.replaceChildren();
    if (!host.name) {
      el.append(h("p", { class: "leer" }, "Noch keine Daten vom Proxmox-Host."));
      return;
    }
    const pveUrl = stand.schnittstellen && stand.schnittstellen.pve_url;
    const hostJob = stand.auftraege.find((a) => a.art === "host-update" && (a.status === "laeuft" || a.status === "wartet"));
    const fehler = host.dienste_fehler || [];
    let updates;
    if (host.updates === null || host.updates === undefined) updates = h("span", { class: "leise-text" }, "Updates: unbekannt");
    else if (host.updates) {
      updates = h("details", { class: "pakete" }, h("summary", {}, `${host.updates} Updates verfügbar`),
        h("ul", {}, (host.update_liste || []).map((p) => h("li", { class: "mono" }, p))));
    } else updates = h("span", { class: "gut" }, "✔ System aktuell");
    el.append(
      h("div", { class: "host-kopf" },
        h("div", {},
          h("h2", {}, `Proxmox-Host ${host.name}`),
          h("p", { class: "leise-text" }, [`Proxmox VE ${host.pve || "?"}`, host.kernel ? `Kernel ${host.kernel}` : null,
            host.uptime ? `läuft seit ${dauer(host.uptime)}` : null].filter(Boolean).join(" · "))),
        pveUrl ? h("a", { href: pveUrl, target: "_blank", rel: "noopener" }, "Proxmox-Oberfläche ↗") : null),
      h("div", { class: "messwerte" },
        messwert("CPU", host.cpu, `${prozent(host.cpu)} von ${host.maxcpu || "?"} Kernen`),
        messwert("Arbeitsspeicher", host.maxmem ? host.mem / host.maxmem : 0, `${bytes(host.mem)} / ${bytes(host.maxmem)}`),
        messwert("System-Disk", host.maxdisk ? host.disk / host.maxdisk : 0, `${bytes(host.disk)} / ${bytes(host.maxdisk)}`)),
      h("div", { class: "host-fuss" },
        updates,
        host.kernel_neustart ? h("span", { class: "mittel" }, "Neuer Kernel – Host bei Gelegenheit neu starten") : null,
        fehler.length ? h("span", { class: "schlecht" }, `Ausgefallen: ${fehler.join(", ")}`)
          : h("span", { class: "gut" }, "✔ Host-Dienste laufen"),
        h("span", { class: "rechts" },
          hostJob ? h("button", { class: "leise", type: "button", onclick: () => protokoll(hostJob.id) }, "⟳ Update läuft – Protokoll")
            : darf("bedienen") ? h("button", { type: "button", disabled: !host.updates, onclick: hostUpdate }, "Host aktualisieren") : null)));
  }

  function dienstChips(g) {
    const pr = g.probe;
    const sichtbar = pr.dienste.filter((d) => d.zustand !== "ruhe");
    const ruhend = pr.dienste.filter((d) => d.zustand === "ruhe");
    const chips = sichtbar.map((d) => {
      const name = d.name.replace(/\.service$/, "");
      const titel = `${d.name}: ${d.text}`;
      if (g.lokal && !g.auftrag && d.name.endsWith(".service") && darf("bedienen")) {
        return h("button", { type: "button", class: `chip ${d.zustand}`, title: `${titel} – klicken zum Neustarten`,
          onclick: () => dienstNeustart(g, d.name) }, d.zustand === "fehler" ? `✘ ${name}` : name);
      }
      return h("span", { class: `chip ${d.zustand}`, title: titel }, name);
    });
    if (ruhend.length) {
      chips.push(h("span", { class: "chip ruhe", title: ruhend.map((d) => `${d.name}: ${d.text}`).join("\n") },
        `${ruhend.length} planmäßig ruhend`));
    }
    return chips.length ? h("div", { class: "dienste", "aria-label": "Dienste" }, chips) : null;
  }

  function infos(g) {
    const pr = g.probe;
    const zeilen = [];
    if (pr.pruefungen.length) {
      zeilen.push(h("dt", {}, "Funktion"), h("dd", {}, pr.pruefungen.map((p, i) => [i ? " · " : "",
        p.ok ? h("span", { class: "gut", title: p.detail }, `✔ antwortet${p.ms !== null ? ` (${p.ms} ms)` : ""}`)
          : h("span", { class: "schlecht" }, `✘ ${p.detail}`)]),
        g.pruefung_eigen ? h("span", { class: "leise-text", title: g.pruefung_eigen.wert }, " · eigene Prüfung") : null));
    } else if (g.lokal && darf("bedienen")) {
      zeilen.push(h("dt", {}, "Funktion"), h("dd", {}, h("span", { class: "leise-text" }, "keine Prüfung · "),
        h("button", { type: "button", class: "leise", onclick: () => pruefungOeffnen(g) }, "festlegen")));
    }
    if (pr.os) zeilen.push(h("dt", {}, "System"), h("dd", {}, pr.os));
    let upd = pr.updates === null || pr.updates === undefined ? h("span", { class: "leise-text" }, "unbekannt")
      : pr.updates ? h("span", { class: "mittel" }, `${pr.updates} verfügbar`) : h("span", { class: "gut" }, "✔ aktuell");
    zeilen.push(h("dt", {}, "Updates"), h("dd", {}, upd, pr.neustart ? h("span", { class: "mittel" }, " · Neustart empfohlen") : null));
    if (g.snapshots && g.snapshots.length) {
      const letzter = g.snapshots.slice().sort().pop().match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})/);
      zeilen.push(h("dt", {}, "Snapshots"), h("dd", {}, `${g.snapshots.length} vor Updates`,
        letzter ? `, zuletzt ${letzter[3]}.${letzter[2]}. ${letzter[4]}:${letzter[5]}` : ""));
    }
    return h("dl", { class: "infos" }, zeilen);
  }

  function autoZeile(g) {
    const pr = g.probe;
    const appMoeglich = pr ? pr.app_update : g.auto.app;
    const aendern = async (system, app) => {
      try {
        await api("/api/auto", { id: g.id, system, app });
        g.auto = { system, app };
        toast(system || app ? `${g.name}: nachts automatisch ${[system && "System", app && "App"].filter(Boolean).join(" und ")}`
          : `${g.name}: keine automatischen Updates`);
        laden();
      } catch (e) { toast(e.message); laden(); }
    };
    const sys = h("input", { type: "checkbox", checked: g.auto.system });
    const app = h("input", { type: "checkbox", checked: g.auto.app, disabled: !appMoeglich && !g.auto.app });
    if (!darf("bedienen")) { sys.disabled = true; app.disabled = true; }
    sys.addEventListener("change", () => aendern(sys.checked, app.checked));
    app.addEventListener("change", () => aendern(sys.checked, app.checked));
    return h("div", { class: "auto" }, h("span", {}, "Nachts automatisch:"),
      h("label", {}, sys, "System"),
      h("label", { title: appMoeglich ? "App-Update wie in der Doku der App" : "Für diesen Container ist kein App-Update eingerichtet" }, app, "App"));
  }

  function menue(titel, eintraege) {
    const d = h("details", { class: "menue" }, h("summary", {}, titel),
      h("div", { class: "liste" }, eintraege.map(([text, fn, an]) => h("button", { type: "button", disabled: an === false,
        onclick: () => { d.open = false; fn(); } }, text))));
    d.addEventListener("toggle", () => { if (!d.open && aufgeschoben) zeichne(); });
    return d;
  }

  function aktionen(g) {
    if (!g.lokal) return h("div", { class: "aktionen leise-text" }, `Läuft auf Knoten ${g.knoten} – dort verwalten.`);
    const pveUrl = stand.schnittstellen && stand.schnittstellen.pve_url;
    if (!darf("bedienen")) {
      return h("div", { class: "aktionen" }, pveUrl ? h("a", { class: "rechts", href: `${pveUrl}/#v1:0:=${g.typ}%2F${g.id}`,
        target: "_blank", rel: "noopener" }, "Proxmox ↗") : null);
    }
    const frei = !g.auftrag;
    const laeuft = g.status === "running";
    const knoepfe = [];
    if (!laeuft) knoepfe.push(h("button", { type: "button", class: "haupt", disabled: !frei, onclick: () => strom(g, "start") }, "Starten"));
    else {
      if (g.typ === "lxc") knoepfe.push(h("button", { type: "button", disabled: !frei, onclick: () => update(g, "alles") }, "Aktualisieren"));
      knoepfe.push(h("button", { type: "button", disabled: !frei, onclick: () => strom(g, "neustart") }, "Neustart"));
      knoepfe.push(h("button", { type: "button", disabled: !frei, onclick: () => strom(g, "herunterfahren") }, "Herunterfahren"));
      const mehr = [];
      if (g.typ === "lxc") {
        mehr.push(["Nur Betriebssystem aktualisieren", () => update(g, "system")]);
        mehr.push(["Nur App aktualisieren", () => update(g, "app"), Boolean(g.probe && g.probe.app_update)]);
      }
      if (g.typ === "lxc") mehr.push(["Funktionsprüfung festlegen …", () => pruefungOeffnen(g)]);
      mehr.push(["Hart ausschalten", () => strom(g, "stopp")]);
      if (frei) knoepfe.push(menue("Mehr", mehr));
    }
    if (pveUrl) {
      knoepfe.push(h("a", { class: "rechts", href: `${pveUrl}/#v1:0:=${g.typ}%2F${g.id}`, target: "_blank", rel: "noopener" }, "Proxmox ↗"));
    }
    return h("div", { class: "aktionen" }, knoepfe);
  }

  function gastKarte(g) {
    const pr = g.probe;
    const laeuft = g.status === "running";
    const job = g.auftrag;
    const gestoert = g.probleme.length > 0;
    const punkt = job ? "arbeit" : gestoert ? "fehler" : laeuft ? "ok" : "";
    const zustand = job ? (job.status === "wartet" ? "Auftrag wartet" : "Auftrag läuft")
      : laeuft ? `läuft seit ${dauer(g.uptime)}` : g.status === "stopped" ? "ausgeschaltet" : g.status;
    const unter = [g.typ === "qemu" ? "VM" : "Container", zustand, pr && pr.ip].filter(Boolean).join(" · ");
    return h("article", { class: `karte gast${gestoert ? " gestoert" : ""}${laeuft ? "" : " aus"}` },
      h("div", { class: "gast-kopf" },
        h("span", { class: `punkt ${punkt}`, title: gestoert ? "gestört" : zustand }),
        h("div", { class: "gast-name" }, h("h3", {}, g.name), h("p", {}, unter)),
        h("div", { class: "marken" }, pr && pr.app_name ? h("span", { class: "marke-klein app" }, pr.app_name) : null,
          h("span", { class: "marke-klein" }, String(g.id)))),
      gestoert ? h("ul", { class: "probleme" }, g.probleme.map((t) => h("li", {}, t))) : null,
      job ? h("div", { class: "arbeit-zeile" }, h("span", {}, `⟳ ${beschreibung(job)}`),
        h("button", { type: "button", class: "leise", onclick: () => protokoll(job.id) }, "Protokoll")) : null,
      laeuft ? h("div", { class: "messwerte" },
        messwert("CPU", g.cpu, prozent(g.cpu)),
        messwert("RAM", g.maxmem ? g.mem / g.maxmem : 0, `${bytes(g.mem)} / ${bytes(g.maxmem)}`),
        g.maxdisk ? messwert("Disk", g.disk / g.maxdisk, `${bytes(g.disk)} / ${bytes(g.maxdisk)}`) : null) : null,
      pr ? dienstChips(g) : null,
      pr ? infos(g) : null,
      laeuft && g.typ === "lxc" && g.lokal && !pr ? h("p", { class: "leise-text" }, "Dienste werden geprüft …") : null,
      g.typ === "lxc" && g.lokal ? autoZeile(g) : null,
      aktionen(g));
  }

  function auftragsListe() {
    const ul = $("auftraege");
    ul.replaceChildren();
    if (!stand.auftraege.length) {
      ul.append(h("li", { class: "leer" }, "Noch keine Aufträge."));
      return;
    }
    for (const a of stand.auftraege) {
      const dauerText = a.ende && a.start ? ` (${dauer(a.ende - a.start)})` : "";
      ul.append(h("li", {},
        h("span", { class: `status-zeichen status-${a.status}`, title: STATUS[a.status] || a.status }, ZEICHEN[a.status] || "?"),
        h("span", { class: "titel" }, beschreibung(a)),
        h("button", { type: "button", class: "leise", onclick: () => protokoll(a.id), title: "Protokoll anzeigen" }, zeit(a.start)),
        a.ergebnis ? h("span", { class: "ergebnis" }, a.ergebnis + dauerText) : null));
    }
  }

  function nachtKarte() {
    const n = stand.nacht || {};
    const e = stand.einstellungen || {};
    const auswahl = stand.gaeste.filter((g) => g.auto.system || g.auto.app);
    const naechster = typeof n.naechster === "number" ? zeit(n.naechster) : n.naechster || "Zeitplan nicht aktiv";
    $("nacht").replaceChildren(h("div", {},
      h("p", {}, h("strong", {}, "Nächster Lauf: "), naechster),
      h("p", { class: auswahl.length ? "" : "leise-text" }, auswahl.length
        ? `Dabei: ${auswahl.map((g) => `${g.name} (${[g.auto.system && "System", g.auto.app && "App"].filter(Boolean).join(" + ")})`).join(", ")}`
        : "Noch kein Container ausgewählt – Häkchen bei „Nachts automatisch“ in der jeweiligen Karte setzen."),
      n.ende ? [h("p", {}, h("strong", {}, "Letzter Lauf: "), `${zeit(n.start)}, `,
        n.ergebnisse.length ? `${n.ergebnisse.length - (n.fehler || 0)} von ${n.ergebnisse.length} ohne Fehler` : "nichts zu tun"),
        n.ergebnisse.length ? h("ul", { class: "nacht-liste" }, n.ergebnisse.map((r) =>
          h("li", {}, h("span", { class: `status-${r.status}` }, `${ZEICHEN[r.status] || "?"} `), `${r.titel}: ${r.ergebnis}`))) : null] : null,
      h("p", { class: "einstellungen" },
        `Snapshot vor jedem Update: ${e.snapshots ? `ja, die letzten ${e.snapshots} bleiben` : "nein"} · `,
        `Neustart, wenn ein Update es verlangt: ${e.neustart ? "ja" : "nein"} · `,
        `Proxmox-Host nachts: ${n.host_auto ? "ja" : "nein"} – einstellbar in `, h("code", {}, "/etc/ecspve/ecspve.env")),
      darf("bedienen") ? h("p", { class: "einstellungen" }, h("button", { type: "button", onclick: auffrischen }, "Paketlisten jetzt neu laden")) : null));
  }

  function kopierZeile(titel, wert, anzeige, text) {
    return h("div", { class: "schnitt-zeile" }, h("strong", {}, titel), h("code", {}, anzeige),
      h("button", { type: "button", onclick: () => kopieren(wert) }, "Kopieren"), h("p", {}, text));
  }

  function schnittstellen() {
    const basis = location.origin;
    const token = (stand.schnittstellen && stand.schnittstellen.token) || "";
    const verdeckt = tokenSichtbar ? token : "•".repeat(12);
    const knopf = h("button", { type: "button", class: "leise", onclick: () => { tokenSichtbar = !tokenSichtbar; schnittstellen(); } },
      tokenSichtbar ? "Token verbergen" : "Token anzeigen");
    $("schnittstellen").replaceChildren(h("div", { class: "schnitt" },
      kopierZeile("Funktionsprüfung", `${basis}/health`, `${basis}/health`,
        "Antwortet 200 „OK“ oder 503 „FEHLER“ – ohne Anmeldung. Für Uptime Kuma (HTTP-Monitor), Checkmk, Zabbix, Nagios oder jede einfache HTTP-Prüfung."),
      kopierZeile("Status als JSON", `${basis}/api/status`, `${basis}/api/status`,
        "Alle Werte dieser Seite – mit dem Token als Header (unten). Uptime Kuma: „HTTP(s) – Json Query“, Ausdruck ok, erwarteter Wert true, unter Headers die Zeile darunter."),
      kopierZeile("Header", `{"Authorization": "Bearer ${token}"}`, `{"Authorization": "Bearer ${verdeckt}"}`,
        "So geht das Token mit – als Header statt in der Adresse, damit es nicht in Protokollen landet."),
      kopierZeile("Prometheus", `${basis}/metrics`, `${basis}/metrics`,
        "Metriken für Prometheus, Grafana Alloy, VictoriaMetrics oder Netdata – Token als Bearer (authorization: credentials)."),
      kopierZeile("Token", token, verdeckt, "Neu erzeugen im Container der Übersicht: ecspve token --neu"),
      h("div", {}, knopf)));
  }

  // --- Aktionen ------------------------------------------------------------------------------------
  async function auftrag(pfad, daten, meldung) {
    try {
      const erg = await api(pfad, daten);
      toast(meldung || "Auftrag gestartet");
      laden();
      return erg;
    } catch (e) { toast(e.message); return null; }
  }
  function strom(g, aktion) {
    const fragen = {
      herunterfahren: `„${g.name}“ herunterfahren?`,
      neustart: `„${g.name}“ neu starten? Die Dienste darin sind kurz nicht erreichbar.`,
      stopp: `„${g.name}“ hart ausschalten? Das ist wie Stecker ziehen – nur nehmen, wenn Herunterfahren nicht klappt.`,
    };
    if (fragen[aktion] && !confirm(fragen[aktion])) return;
    auftrag("/api/aktion", { id: g.id, aktion }, `${g.name}: ${STROM[aktion]} gestartet`);
  }
  function update(g, was) {
    const e = stand.einstellungen || {};
    const snap = e.snapshots ? "\n\nVorher wird ein Snapshot angelegt (falls der Speicher das kann)." : "";
    if (!confirm(`„${g.name}“ aktualisieren (${WAS[was]})?${snap}`)) return;
    auftrag("/api/aktion", { id: g.id, aktion: "update", was }, `${g.name}: Update eingeplant`);
  }
  function dienstNeustart(g, dienst) {
    if (!confirm(`Dienst „${dienst}“ in „${g.name}“ neu starten?`)) return;
    auftrag("/api/aktion", { id: g.id, aktion: "dienst", dienst }, `${dienst}: Neustart gestartet`);
  }
  function hostUpdate() {
    if (!confirm("Proxmox-Host aktualisieren (apt-get dist-upgrade)?\n\nDie Container laufen dabei weiter. Bei einem neuen Kernel ist danach ein Neustart des Hosts nötig – den bitte selbst zu einer ruhigen Zeit über die Proxmox-Oberfläche auslösen.")) return;
    auftrag("/api/host", { aktion: "update" }, "Host-Update gestartet");
  }
  function auffrischen() {
    auftrag("/api/host", { aktion: "auffrischen" }, "Paketlisten werden neu geladen");
  }
  async function alleAktualisieren() {
    const zahl = stand.gaeste.filter((g) => g.typ === "lxc" && g.lokal && g.status === "running" && !g.auftrag).length;
    if (!zahl) { toast("Kein laufender Container frei."); return; }
    if (!confirm(`Alle ${zahl} laufenden Container nacheinander aktualisieren (System und App)?`)) return;
    const erg = await auftrag("/api/alle", {}, "Updates eingeplant");
    if (erg && erg.fehler && erg.fehler.length) toast(`Nicht möglich: ${erg.fehler.join("; ")}`);
  }

  // --- Protokoll -----------------------------------------------------------------------------------
  async function protokoll(id) {
    const d = $("protokoll");
    clearTimeout(protokollTimer);
    if (!d.open) {
      $("protokoll-titel").textContent = "Protokoll";
      $("protokoll-status").textContent = "Wird geladen …";
      $("protokoll-text").textContent = "";
      d.showModal();
    }
    try {
      const a = await api(`/api/auftrag/${encodeURIComponent(id)}`);
      const pre = $("protokoll-text");
      const unten = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      $("protokoll-titel").textContent = beschreibung(a);
      $("protokoll-status").replaceChildren(
        h("span", { class: `status-${a.status}` }, `${ZEICHEN[a.status] || ""} ${STATUS[a.status] || a.status}`),
        ` · gestartet ${zeit(a.start)}`, a.ende ? ` · Dauer ${dauer(a.ende - a.start)}` : "", a.ergebnis ? ` · ${a.ergebnis}` : "");
      const geheim = $("protokoll-geheim");
      geheim.hidden = !a.geheim;
      if (a.geheim) {
        geheim.replaceChildren(...Object.entries(a.geheim).map(([k, v]) => h("p", {}, `${k}: `, h("code", {}, v))),
          h("p", { class: "leise-text" }, "Jetzt notieren – damit meldet ihr euch in der Proxmox-Konsole des Containers als root an."));
      }
      pre.textContent = a.protokoll || "(noch keine Ausgabe)";
      if (unten) pre.scrollTop = pre.scrollHeight;
      if ((a.status === "laeuft" || a.status === "wartet") && d.open) protokollTimer = setTimeout(() => protokoll(id), 2000);
    } catch (e) {
      $("protokoll-status").textContent = e.message;
    }
  }

  // --- Container anlegen ----------------------------------------------------------------------------
  async function anlegenOeffnen() {
    const d = $("anlegen-dialog");
    const f = $("anlegen-form");
    f.reset();
    $("ip-fest").hidden = true;
    $("anlegen-fehler").textContent = "";
    $("anlegen-info").textContent = "Speicher und Netzwerk werden geladen …";
    d.showModal();
    f.elements.name.focus();
    try {
      const o = await api("/api/anlegen");
      f.elements.storage.replaceChildren(...o.speicher.map((s) =>
        h("option", { value: s.name }, `${s.name}${s.frei ? ` (${bytes(s.frei)} frei)` : ""}`)));
      f.elements.bridge.replaceChildren(...o.bruecken.map((b) => h("option", { value: b, selected: b === "vmbr0" }, b)));
      f.elements.ctid.placeholder = `automatisch (${o.naechste_id})`;
      const v = (o.vorlage || "").match(/^debian-(\d+)-standard_([^_]+)_/);
      $("anlegen-info").textContent = `${v ? `Debian ${v[1]} (Vorlage ${v[2]})` : "Debian"}, unprivilegiert – wie die Container der Apps.`;
    } catch (e) {
      $("anlegen-info").textContent = "";
      $("anlegen-fehler").textContent = e.message;
    }
  }
  async function anlegenAbschicken(ev) {
    ev.preventDefault();
    const f = $("anlegen-form");
    const w = (n) => f.elements[n].value.trim();
    const fehler = $("anlegen-fehler");
    fehler.textContent = "";
    if (!/^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(w("name"))) {
      fehler.textContent = "Name: nur Kleinbuchstaben, Ziffern und Bindestrich, z. B. „homeassistant“.";
      f.elements.name.focus();
      return;
    }
    const fest = f.elements.ipart.value === "fest";
    const daten = {
      name: w("name"), ctid: w("ctid"), cores: w("cores"), memory: w("memory"), disk: w("disk"),
      storage: w("storage"), bridge: w("bridge"), vlan: w("vlan"),
      ip: fest ? w("ip") : "dhcp", gw: fest ? w("gw") : "",
      onboot: f.elements.onboot.checked, start: f.elements.start.checked,
    };
    const knopf = f.querySelector("button[type=submit]");
    knopf.disabled = true;
    try {
      const job = await api("/api/anlegen", daten);
      $("anlegen-dialog").close();
      toast(`${daten.name}: wird angelegt`);
      laden();
      protokoll(job.id);
    } catch (e) { fehler.textContent = e.message; }
    finally { knopf.disabled = false; }
  }

  // --- Eigene Funktionsprüfung ---------------------------------------------------------------------
  let pruefGast = null;
  function pruefungOeffnen(g) {
    pruefGast = g;
    const f = $("pruefung-form");
    const eigen = g.pruefung_eigen;
    const ausDatei = eigen && eigen.quelle === "helfer.conf";
    $("pruefung-titel").textContent = `Funktionsprüfung · ${g.name}`;
    f.elements.adresse.value = eigen ? eigen.wert : "";
    f.elements.adresse.disabled = ausDatei;
    f.querySelector("button[type=submit]").disabled = ausDatei;
    $("pruefung-app").textContent = g.probe && g.probe.app_name ? ` (dann gilt wieder die eingebaute Prüfung der App ${g.probe.app_name})` : "";
    $("pruefung-fehler").textContent = ausDatei ? "Diese Prüfung steht in /etc/ecspve/helfer.conf auf dem Proxmox-Host – dort ändern." : "";
    $("pruefung-dialog").showModal();
    if (!ausDatei) f.elements.adresse.focus();
  }
  async function pruefungAbschicken(ev) {
    ev.preventDefault();
    const adresse = $("pruefung-form").elements.adresse.value.trim();
    try {
      await api("/api/pruefung", { id: pruefGast.id, adresse });
      $("pruefung-dialog").close();
      toast(adresse ? `${pruefGast.name}: Prüfung gespeichert – Ergebnis in etwa einer Minute` : `${pruefGast.name}: eigene Prüfung entfernt`);
      laden();
    } catch (e) { $("pruefung-fehler").textContent = e.message; }
  }

  // --- Start ---------------------------------------------------------------------------------------
  document.addEventListener("DOMContentLoaded", () => {
    $("anmeldeform").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      $("anmeldefehler").textContent = "";
      try {
        const d = await api("/api/anmelden", { benutzer: $("benutzername").value.trim().toLowerCase(), passwort: $("passwort").value });
        $("passwort").value = "";
        weiterNach(d.schritt);
      } catch (e) { $("anmeldefehler").textContent = e.message; $("passwort").select(); }
    });
    $("neupwform").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      $("anmeldefehler").textContent = "";
      if ($("neupw1").value !== $("neupw2").value) { $("anmeldefehler").textContent = "Die Passwörter stimmen nicht überein."; return; }
      try {
        weiterNach((await api("/api/anmelden/neues-passwort", { passwort: $("neupw1").value })).schritt);
      } catch (e) {
        $("anmeldefehler").textContent = e.message;
        if (/Startpasswort anmelden/.test(e.message)) anmeldeSchritt("passwort");
      }
    });
    $("codeform").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      $("anmeldefehler").textContent = "";
      try {
        const d = await api("/api/anmelden/code", { code: $("code").value });
        if (d.notfall) toast(`Mit Notfallcode angemeldet – noch ${d.notfall_uebrig} übrig`);
        laden();
      } catch (e) {
        $("anmeldefehler").textContent = e.message;
        $("code").select();
        if (/Passwort/.test(e.message)) anmeldeSchritt("passwort");
      }
    });
    $("einrichtenform").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      $("anmeldefehler").textContent = "";
      try {
        notfallZeigen((await api("/api/zwei-faktor/bestaetigen", { code: $("einrichten-code").value })).notfallcodes);
      } catch (e) { $("anmeldefehler").textContent = e.message; $("einrichten-code").select(); }
    });
    $("notfall-weiter").addEventListener("click", laden);
    document.querySelector("[data-neu-anmelden]").addEventListener("click", async () => {
      try { await api("/api/abmelden", {}); } catch (e) { /* egal */ }
      anmeldeSchritt("passwort");
    });
    $("zf-form").addEventListener("submit", zfAbschicken);
    $("abmelden").addEventListener("click", async () => {
      try { await api("/api/abmelden", {}); } catch (e) { /* egal */ }
      zeigeAnmeldung();
    });
    $("pruefen").addEventListener("click", () => auftrag("/api/pruefen", {}, "Prüfung läuft – Ergebnis in etwa einer Minute"));
    $("alle").addEventListener("click", alleAktualisieren);
    $("benutzer-neu").addEventListener("click", benutzerNeuOeffnen);
    $("benutzer-form").addEventListener("submit", benutzerNeuAbschicken);
    $("konto-form").addEventListener("submit", kontoAbschicken);
    $("anlegen").addEventListener("click", anlegenOeffnen);
    $("anlegen-form").addEventListener("submit", anlegenAbschicken);
    $("pruefung-form").addEventListener("submit", pruefungAbschicken);
    for (const r of $("anlegen-form").elements.ipart) {
      r.addEventListener("change", () => { $("ip-fest").hidden = $("anlegen-form").elements.ipart.value !== "fest"; });
    }
    document.querySelectorAll("[data-schliessen]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));
    $("protokoll-zu").addEventListener("click", () => $("protokoll").close());
    $("protokoll").addEventListener("close", () => clearTimeout(protokollTimer));
    document.addEventListener("visibilitychange", () => { if (!document.hidden && $("anmeldung").hidden) laden(); });
    laden();
  });
})();
