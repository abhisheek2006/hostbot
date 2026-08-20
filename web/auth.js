/* HostBot web API client (login + dashboard) */
(function () {
    "use strict";

    var API = window.HostBotAPI || {};

    API.base = (window.HOSTBOT_API_URL || "").trim() || location.origin;
    // When true, the client falls back to local demo data whenever the real
    // status server is unreachable (network error) or missing (404/405).
    // Set window.HOSTBOT_DEMO_MODE = false once your VPS status server is live.
    API.demoMode = window.HOSTBOT_DEMO_MODE === true;

    var demoStart = Date.now();

    function demoDashboard() {
        var mins = Math.floor((Date.now() - demoStart) / 60000);
        var running = 1 + (mins % 3);
        var now = Date.now();
        return {
            username: "demo_user",
            display_name: "Demo User",
            telegram_id: 1760943918,
            plan: "pro",
            plan_label: "Pro",
            registered_plan: "pro",
            limit: 20,
            expires: null,
            locked: false,
            files_count: 3,
            running_count: running,
            pending_count: 1,
            files: [
                {
                    file_name: "demo_bot.py",
                    file_type: "py",
                    status: "approved",
                    running: running >= 1,
                    pid: 31041,
                    start_time: new Date(now - 3600e3).toISOString(),
                    log_exists: true
                },
                {
                    file_name: "announcer.py",
                    file_type: "py",
                    status: "pending",
                    running: false,
                    pid: null,
                    start_time: null,
                    log_exists: false
                },
                {
                    file_name: "webhook.js",
                    file_type: "js",
                    status: "approved",
                    running: running >= 2,
                    pid: 31055,
                    start_time: new Date(now - 7200e3).toISOString(),
                    log_exists: true
                }
            ]
        };
    }

    /* Local mock handlers - only used when the real server is unreachable. */
    API._demo = async function (path, opts) {
        if (!API.demoMode) return undefined;
        var method = (opts && opts.method) || "GET";
        var urlPath = String(path).split("?")[0];

        if (urlPath === "/api/login" && method === "POST") {
            var token = "demo_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
            return { ok: true, token: token, dashboard: demoDashboard() };
        }
        if (urlPath === "/api/register" && method === "POST") {
            return { ok: true, message: "Account created. (Demo mode - log in with any username/password.)" };
        }
        if (urlPath === "/api/logout") {
            return { ok: true };
        }
        if (urlPath === "/api/dashboard") {
            return demoDashboard();
        }
        if (urlPath === "/api/logs") {
            return {
                ok: true,
                logs: "2026-08-20 12:00:00 [INFO] Demo bot started\n" +
                      "2026-08-20 12:00:01 [INFO] Polling Telegram updates...\n" +
                      "2026-08-20 12:00:05 [INFO] New message received\n" +
                      "2026-08-20 12:00:06 [INFO] Processed update #42\n" +
                      "2026-08-20 12:00:07 [INFO] All systems normal\n" +
                      "\n(Sample logs - connect your VPS status server for real logs.)",
                truncated: false
            };
        }
        if (urlPath === "/api/env") {
            if (method === "GET") {
                return { ok: true, env: { BOT_TOKEN: "123456789:demo-token", CHANNEL_ID: "-1001234567890" } };
            }
            return { ok: true, message: "Environment updated (demo mode)." };
        }
        if (urlPath === "/api/bot" && method === "POST") {
            var action = opts && opts.body && opts.body.action;
            return { ok: true, message: "'" + (action || "bot") + "' requested (demo mode)." };
        }
        return undefined;
    };

    API.getToken = function () {
        return localStorage.getItem("hostbot_token") || "";
    };

    API.setToken = function (t) {
        localStorage.setItem("hostbot_token", t);
    };

    API.clearToken = function () {
        localStorage.removeItem("hostbot_token");
    };

    API.api = async function (path, opts) {
        opts = opts || {};
        opts.headers = opts.headers || {};
        var token = API.getToken();
        if (token) opts.headers["Authorization"] = "Bearer " + token;
        if (opts.body && typeof opts.body === "object") {
            opts.body = JSON.stringify(opts.body);
            opts.headers["Content-Type"] = "application/json";
        }

        var res = null;
        var data = null;
        try {
            res = await fetch(API.base + path, opts);
            try { data = await res.json(); } catch (e) { /* no body */ }
        } catch (e) {
            // Network error / CORS / unreachable server -> demo fallback
            var nfb = await API._demo(path, opts);
            if (nfb !== undefined) return nfb;
            var nerr = new Error("Request failed (" + (e.message || "network error") + ")");
            nerr.status = 0;
            throw nerr;
        }

        if (!res.ok) {
            // 404/405 means there is no API on this host (e.g. still pointing at
            // the static site). Fall back to demo data instead of failing.
            if (res.status === 404 || res.status === 405) {
                var dfb = await API._demo(path, opts);
                if (dfb !== undefined) return dfb;
            }
            var err = new Error((data && data.error) || "Request failed (" + res.status + ")");
            err.status = res.status;
            throw err;
        }
        return data;
    };

    API.login = function (username, password) {
        return API.api("/api/login", { method: "POST", body: { username: username, password: password } });
    };

    API.logout = function () {
        var token = API.getToken();
        API.clearToken();
        if (token) {
            API.api("/api/logout", { method: "POST", body: { token: token } }).catch(function () {});
        }
    };

    API.dashboard = function () {
        return API.api("/api/dashboard");
    };

    API.logs = function (file) {
        return API.api("/api/logs?file=" + encodeURIComponent(file));
    };

    API.envGet = function (file) {
        return API.api("/api/env?file=" + encodeURIComponent(file));
    };

    API.envSet = function (file, env) {
        return API.api("/api/env", { method: "POST", body: { file: file, env: env } });
    };

    API.botAction = function (file, action) {
        return API.api("/api/bot", { method: "POST", body: { file: file, action: action } });
    };

    window.HostBotAPI = API;
})();