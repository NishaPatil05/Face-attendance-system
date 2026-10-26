/* Face Recognition Attendance System - frontend (plain JavaScript, no frameworks). */
(() => {
  "use strict";

  const TOKEN = document.querySelector('meta[name="app-token"]').content;
  const $ = (selector, root = document) => root.querySelector(selector);

  /* ------------------------------------------------------------------ *
   * Helpers
   * ------------------------------------------------------------------ */
  const pad = (n) => String(n).padStart(2, "0");
  const formatDate = (d) => `${pad(d.getDate())}-${pad(d.getMonth() + 1)}-${d.getFullYear()}`;
  const formatTime = (d) => {
    const hours = d.getHours();
    return `${pad(hours % 12 || 12)}:${pad(d.getMinutes())}:${pad(d.getSeconds())} ${hours < 12 ? "AM" : "PM"}`;
  };

  /** Call the local API. Throws Error(message) with a user-friendly message. */
  async function api(path, { method = "GET", body } = {}) {
    const options = { method, headers: { "X-App-Token": TOKEN } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch {
      throw new Error("Cannot reach the application. Is it still running?");
    }
    let data = {};
    try { data = await response.json(); } catch { /* empty body */ }
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function toast(message, type = "info") {
    const element = document.createElement("div");
    element.className = `toast ${type}`;
    element.textContent = message;
    $("#toasts").appendChild(element);
    setTimeout(() => element.remove(), type === "error" ? 7000 : 4000);
  }

  /** Modal dialog. Resolves true for OK, false for Cancel/Esc. */
  function dialog(title, message, { okText = "OK", cancel = true, danger = false } = {}) {
    const box = $("#dialog");
    return new Promise((resolve) => {
      $("#dialog-title").textContent = title;
      $("#dialog-message").textContent = message;
      const ok = $("#dialog-ok");
      ok.textContent = okText;
      ok.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
      box.querySelector('button[value="cancel"]').hidden = !cancel;
      box.returnValue = "";
      box.addEventListener("close", () => resolve(box.returnValue === "ok"), { once: true });
      box.showModal();
    });
  }

  /** Fill a table body. ``rows`` is an array of arrays of text values. */
  function fillTable(tbody, rows, columns, emptyText) {
    tbody.replaceChildren();
    if (!rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = columns;
      td.className = "empty";
      td.textContent = emptyText;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }
    for (const row of rows) {
      const tr = document.createElement("tr");
      for (const value of row) {
        const td = document.createElement("td");
        if (value instanceof Node) td.appendChild(value); else td.textContent = value;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
  }

  async function downloadFile(path, fallbackName) {
    let response;
    try {
      response = await fetch(path, { headers: { "X-App-Token": TOKEN } });
    } catch {
      throw new Error("Cannot reach the application. Is it still running?");
    }
    if (!response.ok) {
      let message = "Download failed.";
      try { message = (await response.json()).error || message; } catch { /* ignore */ }
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = match ? decodeURIComponent(match[1]) : fallbackName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  }

  const streamUrl = (path) => `${path}?token=${encodeURIComponent(TOKEN)}&t=${Date.now()}`;
  const goTo = (name) => { location.hash = `#/${name}`; };

  /* ------------------------------------------------------------------ *
   * Dashboard
   * ------------------------------------------------------------------ */
  const dashboard = {
    timer: null,
    enter() {
      this.refresh();
      this.timer = setInterval(() => this.refresh(), 5000);
    },
    leave() { clearInterval(this.timer); },
    async refresh() {
      try {
        const data = await api("/api/dashboard");
        $("#stat-registered").textContent = data.registered;
        $("#stat-present").textContent = data.present_today;
      } catch {
        $("#stat-registered").textContent = "-";
        $("#stat-present").textContent = "-";
      }
    },
  };

  function tickClock() {
    const now = new Date();
    $("#stat-date").textContent = formatDate(now);
    $("#stat-time").textContent = formatTime(now);
  }

  $("#btn-exit").addEventListener("click", async () => {
    if (!(await dialog("Exit", "Are you sure you want to exit the application?", { okText: "Exit", danger: true }))) return;
    try { await api("/api/shutdown", { method: "POST" }); } catch { /* server may close first */ }
    document.body.innerHTML =
      '<main style="text-align:center;padding:80px 20px"><h2>Application closed</h2>' +
      "<p>The camera has been released. You can close this browser tab.</p></main>";
  });

  /* ------------------------------------------------------------------ *
   * Live attendance
   * ------------------------------------------------------------------ */
  const RESULT_SECONDS = 4;
  const attendance = {
    timer: null, hideTimer: null, since: 0, queue: [], showing: false, polling: false,

    setStatus(tone, banner, l1 = "", l2 = "", l3 = "") {
      $("#att-status").dataset.tone = tone;
      $("#att-banner").textContent = banner;
      $("#att-line1").textContent = l1;
      $("#att-line2").textContent = l2;
      $("#att-line3").textContent = l3;
    },
    setIdle(registered) {
      if (registered === 0) this.setStatus("idle", "Waiting for a face\u2026", "No people are registered yet.", "Use \u201cRegister Person\u201d from the dashboard.");
      else this.setStatus("idle", "Waiting for a face\u2026", "Look at the camera to mark attendance.");
    },
    reset() {
      clearInterval(this.timer); clearTimeout(this.hideTimer);
      this.since = 0; this.queue = []; this.showing = false; this.polling = false;
      $("#att-history").replaceChildren();
      $("#att-counters").textContent = "\u00a0";
      $("#att-feed").hidden = true;
      $("#att-feed").removeAttribute("src");
      $("#att-placeholder").hidden = false;
      $("#att-placeholder").textContent = "Starting camera\u2026";
      this.setStatus("idle", "Waiting for a face\u2026");
    },
    async enter() {
      this.reset();
      let started;
      try {
        started = await api("/api/attendance/start", { method: "POST" });
      } catch (error) {
        toast(error.message, "error");
        goTo("dashboard");
        return;
      }
      this.registered = started.registered;
      this.setIdle(started.registered);
      const feed = $("#att-feed");
      feed.onerror = () => { feed.hidden = true; $("#att-placeholder").hidden = false; $("#att-placeholder").textContent = "Video stream interrupted."; };
      feed.src = streamUrl("/api/attendance/stream");
      feed.hidden = false;
      $("#att-placeholder").hidden = true;
      this.timer = setInterval(() => this.poll(), 700);
    },
    async leave() {
      this.reset();
      try { await api("/api/attendance/stop", { method: "POST" }); } catch { /* ignore */ }
    },
    async poll() {
      if (this.polling) return;
      this.polling = true;
      try {
        const data = await api(`/api/attendance/status?since=${this.since}`);
        if (!data.running) {
          toast(data.error || "The camera stopped.", "error");
          goTo("dashboard");
          return;
        }
        $("#att-counters").textContent = `Faces in view: ${data.faces}   |   Registered people: ${data.registered}`;
        this.registered = data.registered;
        for (const event of data.events) {
          this.since = Math.max(this.since, event.id);
          this.queue.push(event);
          this.addHistory(event);
        }
        this.pump();
      } catch (error) {
        toast(error.message, "error");
      } finally {
        this.polling = false;
      }
    },
    pump() {
      if (this.showing || !this.queue.length) return;
      this.showing = true;
      this.show(this.queue.shift());
      this.hideTimer = setTimeout(() => {
        this.showing = false;
        this.setIdle(this.registered);
        this.pump();
      }, RESULT_SECONDS * 1000);
    },
    show(event) {
      if (event.status === "marked") {
        this.setStatus("success", "\u2713 Attendance Marked", `Name: ${event.name}`, `Time: ${event.time}`, "Status: Present");
      } else if (event.status === "already_present") {
        this.setStatus("warning", "Already Present Today", `Name: ${event.name}`, event.time ? `First marked at: ${event.time}` : "", "Status: Present");
      } else {
        this.setStatus("error", "Attendance not saved", event.message, `${event.name} will be retried automatically.`);
      }
    },
    addHistory(event) {
      const outcomes = { marked: "Marked present", already_present: "Already present", excel_error: "Excel error", database_error: "Database error" };
      const item = document.createElement("li");
      item.textContent = `${event.stamp}   ${event.name} (${event.employee_id}) \u2013 ${outcomes[event.status] || event.status}`;
      const list = $("#att-history");
      list.prepend(item);
      while (list.children.length > 50) list.lastChild.remove();
    },
  };

  /* ------------------------------------------------------------------ *
   * Register person
   * ------------------------------------------------------------------ */
  const register = {
    timer: null, polling: false,
    form: () => $("#reg-form"),
    values() {
      const data = new FormData(this.form());
      return Object.fromEntries(data.entries());
    },
    setInputsEnabled(enabled) {
      for (const input of this.form().querySelectorAll("input")) input.disabled = !enabled;
    },
    showPlaceholder(html) {
      $("#reg-feed").hidden = true;
      $("#reg-feed").removeAttribute("src");
      $("#reg-placeholder").hidden = false;
      $("#reg-placeholder").innerHTML = html;      // static text only, never user input
    },
    setProgress(done, total) {
      $("#reg-progress").max = total;
      $("#reg-progress").value = done;
      $("#reg-progress-text").textContent = `Samples captured: ${done} / ${total}`;
    },
    reset() {
      clearInterval(this.timer); this.polling = false;
      this.form().reset();
      this.setInputsEnabled(true);
      $("#reg-capture").textContent = "Capture Face";
      $("#reg-capture").disabled = false;
      $("#reg-save").disabled = true;
      $("#reg-instruction").textContent = "Fill in the details, then click Capture Face.";
      $("#reg-status").textContent = "\u00a0";
      this.setProgress(0, 5);
      this.showPlaceholder("Camera preview<br><br>Click \u201cCapture Face\u201d to begin");
    },
    enter() { this.reset(); },
    async leave() {
      clearInterval(this.timer);
      this.showPlaceholder("");
      try { await api("/api/register/cancel", { method: "POST" }); } catch { /* ignore */ }
    },
    async capture() {
      const button = $("#reg-capture");
      button.disabled = true;
      $("#reg-save").disabled = true;
      try {
        const started = await api("/api/register/start", { method: "POST", body: this.values() });
        this.setProgress(0, started.total);
      } catch (error) {
        toast(error.message, "error");
        button.disabled = false;
        return;
      }
      this.setInputsEnabled(false);
      const feed = $("#reg-feed");
      feed.src = streamUrl("/api/register/stream");
      feed.hidden = false;
      $("#reg-placeholder").hidden = true;
      this.timer = setInterval(() => this.poll(), 500);
    },
    async poll() {
      if (this.polling) return;
      this.polling = true;
      try {
        const s = await api("/api/register/status");
        this.setProgress(s.samples, s.total);
        if (s.state === "capturing") {
          $("#reg-instruction").textContent = s.instruction;
          $("#reg-status").textContent = s.message || "\u00a0";
        } else if (s.state === "complete") {
          clearInterval(this.timer);
          $("#reg-instruction").textContent = s.instruction;
          $("#reg-status").textContent = s.message;
          this.showPlaceholder("\u2713 Face captured<br><br>No photo is stored.<br>Only a numeric face signature.");
          this.setInputsEnabled(true);
          $("#reg-capture").textContent = "Re-capture Face";
          $("#reg-capture").disabled = false;
          $("#reg-save").disabled = false;
        } else if (s.state === "error") {
          clearInterval(this.timer);
          this.setInputsEnabled(true);
          this.setProgress(0, s.total);
          $("#reg-capture").textContent = "Capture Face";
          $("#reg-capture").disabled = false;
          $("#reg-instruction").textContent = "Capture stopped. Click Capture Face to try again.";
          $("#reg-status").textContent = "\u00a0";
          this.showPlaceholder("Capture stopped");
          await dialog("Capture problem", s.error || "Capture failed.", { cancel: false });
        }
      } catch (error) {
        clearInterval(this.timer);
        toast(error.message, "error");
      } finally {
        this.polling = false;
      }
    },
    async save() {
      $("#reg-save").disabled = true;
      try {
        const result = await api("/api/register/save", { method: "POST", body: this.values() });
        await dialog("Registration", result.message, { cancel: false });
        goTo("dashboard");
      } catch (error) {
        toast(error.message, "error");
        $("#reg-save").disabled = false;
      }
    },
  };

  /* ------------------------------------------------------------------ *
   * Attendance records
   * ------------------------------------------------------------------ */
  const records = {
    query() {
      return new URLSearchParams({ date: $("#rec-date").value.trim(), search: $("#rec-search").value.trim() });
    },
    enter() { $("#rec-search").value = ""; this.today(); },
    today() { $("#rec-date").value = formatDate(new Date()); return this.load(); },
    all() { $("#rec-date").value = ""; return this.load(); },
    async load() {
      try {
        const data = await api(`/api/records?${this.query()}`);
        fillTable($("#rec-body"), data.records.map((r) => [r.employee_id, r.name, r.department, r.date, r.time, r.status]), 6, "No attendance records found.");
        $("#rec-count").textContent = `${data.records.length} record(s) shown   |   P = Present`;
      } catch (error) {
        toast(error.message, "error");
      }
    },
    async exportExcel() {
      try {
        await downloadFile(`/api/records/export?${this.query()}`, "attendance_export.xlsx");
        toast("Export complete. A copy was also saved in the exports folder.", "success");
      } catch (error) {
        toast(error.message, "error");
      }
    },
    async openExcel() {
      try {
        await api("/api/excel/open", { method: "POST" });
        toast("Opening attendance.xlsx\u2026", "success");
      } catch (error) {
        toast(error.message, "error");
      }
    },
  };

  /* ------------------------------------------------------------------ *
   * Registered people
   * ------------------------------------------------------------------ */
  const people = {
    timer: null,
    enter() { $("#ppl-search").value = ""; this.load(); },
    async load() {
      try {
        const data = await api(`/api/people?search=${encodeURIComponent($("#ppl-search").value.trim())}`);
        const rows = data.people.map((p) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "btn btn-danger btn-sm";
          button.textContent = "Delete";
          button.addEventListener("click", () => this.remove(p));
          return [p.employee_id, p.name, p.department, p.email || "-", p.registered_on, p.samples, button];
        });
        fillTable($("#ppl-body"), rows, 7, "No registered people found.");
        $("#ppl-count").textContent = `${data.people.length} registered person(s)`;
      } catch (error) {
        toast(error.message, "error");
      }
    },
    async remove(person) {
      const confirmed = await dialog(
        "Delete person",
        `Delete ${person.name} (${person.employee_id})?\n\nTheir face data will be permanently removed and they will no longer be recognised. Past attendance records are kept.`,
        { okText: "Delete", danger: true });
      if (!confirmed) return;
      try {
        await api(`/api/people/${encodeURIComponent(person.employee_id)}`, { method: "DELETE" });
        toast(`${person.name} was deleted.`, "success");
        this.load();
      } catch (error) {
        toast(error.message, "error");
      }
    },
  };

  /* ------------------------------------------------------------------ *
   * Settings
   * ------------------------------------------------------------------ */
  const settings = {
    form: () => $("#set-form"),
    async enter() {
      try {
        const s = await api("/api/settings");
        const f = this.form().elements;
        f.camera_index.value = s.camera_index;
        f.recognition_threshold.value = s.recognition_threshold;
        f.attendance_cooldown_seconds.value = s.attendance_cooldown_seconds;
        f.confirm_frames.value = s.confirm_frames;
        $("#set-threshold-out").textContent = Number(s.recognition_threshold).toFixed(2);
        const list = $("#set-paths");
        list.replaceChildren();
        for (const [label, value] of Object.entries(s.paths)) {
          const dt = document.createElement("dt"); dt.textContent = label;
          const dd = document.createElement("dd"); dd.textContent = value;
          list.append(dt, dd);
        }
      } catch (error) {
        toast(error.message, "error");
      }
    },
    values() {
      const f = this.form().elements;
      return {
        camera_index: f.camera_index.value, recognition_threshold: f.recognition_threshold.value,
        attendance_cooldown_seconds: f.attendance_cooldown_seconds.value, confirm_frames: f.confirm_frames.value,
      };
    },
    async save() {
      try {
        const result = await api("/api/settings", { method: "POST", body: this.values() });
        toast(result.message, "success");
        this.enter();
      } catch (error) {
        toast(error.message, "error");
      }
    },
    async testCamera() {
      const button = $("#set-test");
      button.disabled = true;
      button.textContent = "Testing\u2026";
      try {
        const result = await api("/api/settings/test-camera", { method: "POST", body: { camera_index: this.form().elements.camera_index.value } });
        toast(result.message, "success");
      } catch (error) {
        await dialog("Camera test failed", error.message, { cancel: false });
      } finally {
        button.disabled = false;
        button.textContent = "Test Camera";
      }
    },
  };

  /* ------------------------------------------------------------------ *
   * Router
   * ------------------------------------------------------------------ */
  const views = { dashboard, attendance, register, records, people, settings };
  let current = null;
  let navigation = 0;

  async function navigate() {
    const requested = location.hash.replace(/^#\/?/, "");
    const target = views[requested] ? requested : "dashboard";
    const ticket = ++navigation;
    if (current && views[current].leave) {
      try { await views[current].leave(); } catch { /* ignore */ }
    }
    if (ticket !== navigation) return;          // a newer navigation took over
    document.querySelectorAll(".view").forEach((view) => { view.hidden = true; });
    $(`#view-${target}`).hidden = false;
    document.querySelectorAll("[data-nav]").forEach((link) => link.classList.toggle("active", link.dataset.nav === target));
    current = target;
    if (views[target].enter) views[target].enter();
  }

  /* ------------------------------------------------------------------ *
   * Wiring
   * ------------------------------------------------------------------ */
  $("#reg-capture").addEventListener("click", () => register.capture());
  $("#reg-save").addEventListener("click", () => register.save());
  $("#reg-cancel").addEventListener("click", () => goTo("dashboard"));
  $("#reg-form").addEventListener("submit", (event) => event.preventDefault());

  $("#rec-filters").addEventListener("submit", (event) => { event.preventDefault(); records.load(); });
  $("#rec-today").addEventListener("click", () => records.today());
  $("#rec-all").addEventListener("click", () => records.all());
  $("#rec-export").addEventListener("click", () => records.exportExcel());
  $("#rec-open").addEventListener("click", () => records.openExcel());

  $("#ppl-search").addEventListener("input", () => {
    clearTimeout(people.timer);
    people.timer = setTimeout(() => people.load(), 250);
  });
  $("#ppl-clear").addEventListener("click", () => { $("#ppl-search").value = ""; people.load(); });

  $("#set-form").addEventListener("submit", (event) => { event.preventDefault(); settings.save(); });
  $("#set-test").addEventListener("click", () => settings.testCamera());
  $('#set-form [name="recognition_threshold"]').addEventListener("input", (event) => {
    $("#set-threshold-out").textContent = Number(event.target.value).toFixed(2);
  });

  // Release the camera if the tab is closed or refreshed.
  window.addEventListener("pagehide", () => {
    navigator.sendBeacon(`/api/attendance/stop?token=${encodeURIComponent(TOKEN)}`);
    navigator.sendBeacon(`/api/register/cancel?token=${encodeURIComponent(TOKEN)}`);
  });

  window.addEventListener("hashchange", navigate);
  tickClock();
  setInterval(tickClock, 1000);
  navigate();
})();
