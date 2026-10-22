"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const STROM = { start: "Starten", herunterfahren: "Herunterfahren", neustart: "Neustart", stopp: "Hart ausschalten" };
  const WAS = { alles: "System und App", system: "Betriebssystem", app: "App" };
  const STATUS = { ok: "erfolgreich", warnung: "mit Warnung", fehler: "fehlgeschlagen", laeuft: "läuft", wartet: "wartet" };
  const ZEICHEN = { ok: "✔", warnung: "⚠", fehler: "✘", laeuft: "⟳", wartet: "…" };

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
    if (r.status === 401 && pfad !== "/api/anmelden") {
      zeigeAnmeldung();
      throw new Error("Bitte anmelden.");
    }
    if (!r.ok) throw new Error((json && json.fehler) || `Fehler ${r.status}`);
    return json;
  }
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
  function zeigeAnmeldung() {
    clearTimeout(timer);
    $("inhalt").hidden = true;
    $("gesamt").hidden = true;
    $("abmelden").hidden = true;
    $("anmeldung").hidden = false;
    $("passwort").focus();
  }
  function zeigeInhalt() {
    $("anmeldung").hidden = true;
    $("inhalt").hidden = false;
    $("abmelden").hidden = false;
    $("gesamt").hidden = false;
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
  }

  function meldungen() {
    const el = $("meldungen");
    el.replaceChildren();
    if (stand.probleme.length) {
      el.append(h("div", { class: "meldung fehler" },
        h("h3", {}, "Probleme"), h("ul", {}, stand.probleme.map((p) => h("li", {}, p.text)))));
    }
    if (stand.hinweise.length) {
      el.append(h("div", { class: "meldung hinweis" },
        h("h3", {}, "Hinweise"), h("ul", {}, stand.hinweise.map((t) => h("li", {}, t)))));
    }
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
            : h("button", { type: "button", disabled: !host.updates, onclick: hostUpdate }, "Host aktualisieren"))));
  }

  function dienstChips(g) {
    const pr = g.probe;
    const sichtbar = pr.dienste.filter((d) => d.zustand !== "ruhe");
    const ruhend = pr.dienste.filter((d) => d.zustand === "ruhe");
    const chips = sichtbar.map((d) => {
      const name = d.name.replace(/\.service$/, "");
      const titel = `${d.name}: ${d.text}`;
      if (g.lokal && !g.auftrag && d.name.endsWith(".service")) {
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
    } else if (g.lokal) {
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
      h("p", { class: "einstellungen" }, h("button", { type: "button", onclick: auffrischen }, "Paketlisten jetzt neu laden"))));
  }

  function kopierZeile(titel, wert, anzeige, text) {
    const kopieren = async () => {
      try { await navigator.clipboard.writeText(wert); toast("Kopiert"); }
      catch (e) { toast("Kopieren nicht möglich – bitte markieren und kopieren"); }
    };
    return h("div", { class: "schnitt-zeile" }, h("strong", {}, titel), h("code", {}, anzeige),
      h("button", { type: "button", onclick: kopieren }, "Kopieren"), h("p", {}, text));
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
      kopierZeile("Status als JSON", `${basis}/api/status?token=${token}`, `${basis}/api/status?token=${verdeckt}`,
        "Alle Werte dieser Seite. Uptime Kuma: „HTTP(s) – Json Query“ mit dem Ausdruck ok und dem erwarteten Wert true; auch für Home Assistant oder eigene Skripte."),
      kopierZeile("Prometheus", `${basis}/metrics`, `${basis}/metrics`,
        "Metriken für Prometheus, Grafana Agent/Alloy, VictoriaMetrics oder Netdata – mit dem Token als Bearer (authorization: credentials) oder ?token=…."),
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
        await api("/api/anmelden", { passwort: $("passwort").value });
        $("passwort").value = "";
        laden();
      } catch (e) { $("anmeldefehler").textContent = e.message; }
    });
    $("abmelden").addEventListener("click", async () => {
      try { await api("/api/abmelden", {}); } catch (e) { /* egal */ }
      zeigeAnmeldung();
    });
    $("pruefen").addEventListener("click", () => auftrag("/api/pruefen", {}, "Prüfung läuft – Ergebnis in etwa einer Minute"));
    $("alle").addEventListener("click", alleAktualisieren);
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
