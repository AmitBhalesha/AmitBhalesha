/* Stands in for the Beatline server on a static demo page.
 *
 * `python -m beatline export-demo` precomputes every API response with the real
 * analytics code and writes them to data/*.json. This shim answers the
 * dashboard's /api/* requests from those files, applying the same role rules
 * as the server (officers see only themselves, auditors see only the audit
 * log) and keeping an in-memory audit log of what this viewer looks at.
 */
(function () {
  "use strict";
  const realFetch = window.fetch.bind(window);
  const cache = new Map();
  const audit = [];
  let meta = null;
  let user = null;
  let auditId = 0;

  window.BEATLINE_DEMO = true;

  function json(body, status = 200) {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }

  function load(path) {
    if (!cache.has(path)) {
      cache.set(path, realFetch(`data/${path}`).then((r) => {
        if (!r.ok) throw new Error(`Missing demo data: ${path}`);
        return r.json();
      }));
    }
    return cache.get(path);
  }

  function record(action, detail) {
    audit.push({ id: ++auditId, ts: Math.floor(Date.now() / 1000), username: user?.username ?? null, action, detail, ip: null });
  }

  const forbidden = () => json({ detail: `The ${user.role} role cannot access this` }, 403);

  async function handle(url, opts) {
    meta = meta || (await load("meta.json"));
    const path = url.pathname.replace(/^.*\/api\//, "/api/");
    const q = url.searchParams;
    const body = opts?.body ? JSON.parse(opts.body) : {};

    if (path === "/api/config") return json(meta.config);
    if (path === "/api/login") {
      const u = meta.users[body.username];
      if (!u) return json({ detail: "Unknown demo user" }, 401);
      user = u;
      record("login", { role: u.role });
      return json({ user });
    }
    if (!user) return json({ detail: "Not signed in" }, 401);
    if (path === "/api/logout") {
      record("logout", {});
      user = null;
      return json({ ok: true });
    }
    if (path === "/api/me") return json(user);
    if (path === "/api/beats") return json(meta.beats);
    if (path === "/api/zones") return json(meta.zones);
    if (path === "/api/audit") {
      if (!["auditor", "admin"].includes(user.role)) return forbidden();
      const rows = audit.slice().reverse();
      record("view_audit_log", { filter_username: null });
      return json(rows);
    }

    const ops = ["sergeant", "officer", "admin"].includes(user.role);
    const isOfficer = user.role === "officer";
    if (path === "/api/shifts") {
      if (!ops) return forbidden();
      return json(isOfficer ? meta.officer_shifts : meta.shifts);
    }
    if (!["/api/report", "/api/trails", "/api/heat"].includes(path)) return json({ detail: "Not available in the demo" }, 404);
    if (!ops) return forbidden();

    const start = Number(q.get("start"));
    const end = Number(q.get("end"));
    const file = await load(`shift-${start}.json`);
    if (path === "/api/report") {
      const report = isOfficer ? file.officer_report : file.report;
      if (!report) return json({ detail: "You did not work this shift" }, 404);
      record("view_shift_report", { start, end, officers: report.officers.map((o) => o.officer_id) });
      return json(report);
    }
    if (path === "/api/trails") {
      const requested = q.getAll("officer_id");
      if (isOfficer && requested.some((id) => id !== user.officer_id)) {
        record("denied_trails", { requested });
        return json({ detail: "Officers can only view their own location history" }, 403);
      }
      let trails = file.trails;
      const keep = isOfficer ? [user.officer_id] : requested.length ? requested : null;
      if (keep) trails = Object.fromEntries(Object.entries(trails).filter(([id]) => keep.includes(id)));
      record("view_trails", { start, end, officers: Object.keys(trails).sort() });
      return json(trails);
    }
    const layer = q.get("layer");
    const heat = layer === "calls" ? file.heat_calls : isOfficer ? file.officer_heat_patrol : file.heat_patrol;
    record("view_heat", { layer, start, end });
    return json(heat);
  }

  window.fetch = function (input, opts) {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!/\/api\//.test(url.pathname)) return realFetch(input, opts);
    return handle(url, opts).catch((err) => json({ detail: err.message }, 500));
  };
})();
