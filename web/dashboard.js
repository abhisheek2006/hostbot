/* HostBot dashboard logic */
(function () {
    "use strict";

    var API = window.HostBotAPI;

    if (!API.getToken()) {
        location.replace("login.html");
        return;
    }

    var state = {
        dashboard: null,
        detailFile: null,
        autoRefresh: false,
        refreshTimer: null,
        logTimer: null
    };

    var els = {
        loading: document.getElementById("loading"),
        content: document.getElementById("dashContent"),
        chipName: document.getElementById("chipName"),
        helloName: document.getElementById("helloName"),
        helloMeta: document.getElementById("helloMeta"),
        planLabel: document.getElementById("planLabel"),
        limitLabel: document.getElementById("limitLabel"),
        stFiles: document.getElementById("stFiles"),
        stRunning: document.getElementById("stRunning"),
        stPending: document.getElementById("stPending"),
        stHost: document.getElementById("stHost"),
        stHostUnit: document.getElementById("stHostUnit"),
        botList: document.getElementById("botList"),
        detailPanel: document.getElementById("detailPanel"),
        detailTitle: document.getElementById("detailTitle"),
        logBox: document.getElementById("logBox"),
        logHint: document.getElementById("logHint"),
        envRows: document.getElementById("envRows"),
        toast: document.getElementById("toast")
    };

    function toast(msg, isError) {
        els.toast.textContent = msg;
        els.toast.className = "toast show" + (isError ? " error" : "");
        clearTimeout(toast._t);
        toast._t = setTimeout(function () {
            els.toast.classList.remove("show");
        }, 3200);
    }

    function esc(s) {
        return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    function fileIs(file) {
        return file.status === "approved";
    }

    async function loadDashboard() {
        try {
            var data = await API.dashboard();
            state.dashboard = data;
            renderDashboard();
        } catch (err) {
            if (err.status === 401) {
                API.clearToken();
                location.replace("login.html");
                return;
            }
            toast("Failed to load dashboard: " + (err.message || err), true);
            els.loading.textContent = "Could not reach the HostBot server. Check HOSTBOT_API_URL.";
        }
    }

    function renderDashboard() {
        var d = state.dashboard;
        els.loading.hidden = true;
        els.content.hidden = false;

        els.chipName.textContent = d.display_name || d.username;
        els.helloName.textContent = d.display_name || d.username;
        els.planLabel.textContent = d.plan_label || d.plan;
        els.limitLabel.textContent = d.limit + (d.limit === "Unlimited" ? "" : " bots max");

        var meta = "Telegram ID: " + d.telegram_id;
        if (d.expires) meta += "  |  Plan expires: " + d.expires.split("T")[0];
        if (d.locked) meta += "  |  Host locked";
        if (d.plan_note) meta += "  |  " + d.plan_note;
        els.helloMeta.textContent = meta;

        els.stFiles.textContent = d.files_count;
        els.stRunning.textContent = d.running_count;
        els.stPending.textContent = d.pending_count;
        els.stHost.textContent = d.locked ? "Locked" : "Online";
        els.stHostUnit.textContent = "";

        els.botList.innerHTML = "";
        if (!d.files || d.files.length === 0) {
            els.botList.innerHTML =
                '<div class="empty-state">' +
                '<i class="ph ph-files" aria-hidden="true"></i>' +
                "You have no bots yet.<br>Upload one from the Telegram bot to get started." +
                "</div>";
            return;
        }

        d.files.forEach(function (f) {
            var card = document.createElement("article");
            card.className = "bot-card";

            var statusTag = f.status === "approved" ? "tag-approved" : (f.status === "pending" ? "tag-pending" : "tag-rejected");
            var running = f.running;

            var sub = (f.pid ? "PID " + f.pid : "not running");
            if (f.start_time) sub += "  |  since " + f.start_time.replace("T", " ").slice(0, 19);
            if (f.log_exists) sub += "  |  log file present";

            var actions =
                '<button class="btn btn-primary btn-sm" data-action="start" ' + (running ? "disabled" : "") + '><i class="ph ph-play" aria-hidden="true"></i> Start</button>' +
                '<button class="btn btn-danger btn-sm" data-action="stop" ' + (running ? "" : "disabled") + '><i class="ph ph-stop" aria-hidden="true"></i> Stop</button>' +
                '<button class="btn btn-ghost btn-sm" data-action="restart" ' + (running ? "" : "disabled") + '><i class="ph ph-arrow-clockwise" aria-hidden="true"></i> Restart</button>' +
                '<button class="btn btn-ghost btn-sm" data-action="logs"><i class="ph ph-file-text" aria-hidden="true"></i> Logs</button>' +
                '<button class="btn btn-ghost btn-sm" data-action="env" ' + (fileIs(f) ? "" : "disabled") + '><i class="ph ph-sliders-horizontal" aria-hidden="true"></i> Env</button>' +
                '<button class="btn btn-ghost btn-sm btn-danger-outline" data-action="delete"><i class="ph ph-trash" aria-hidden="true"></i> Delete</button>';

            card.innerHTML =
                '<div class="bot-head">' +
                '<span class="bot-state ' + (running ? "running" : "stopped") + '" aria-hidden="true"></span>' +
                '<span class="bot-name">' + esc(f.file_name) + "</span>" +
                '<span class="tag tag-type">' + esc(f.file_type || "py") + "</span>" +
                '<span class="tag ' + statusTag + '">' + esc(f.status) + "</span>" +
                "</div>" +
                '<div class="bot-sub">' + esc(sub) + "</div>" +
                '<div class="bot-actions">' + actions + "</div>";

            card.addEventListener("click", function (e) {
                var btn = e.target.closest("[data-action]");
                if (!btn || btn.disabled) return;
                var action = btn.getAttribute("data-action");
                if (action === "logs") { showDetail(f.file_name, "logs"); return; }
                if (action === "env") { showDetail(f.file_name, "env"); return; }
                handleBotAction(f.file_name, action);
            });

            els.botList.appendChild(card);
        });
    }

    async function handleBotAction(file, action) {
        if (action === "delete") {
            if (!confirm("Delete '" + file + "'? This stops it and removes the file and logs.")) return;
            try {
                var del = await API.deleteFile(file);
                toast(del.message || "File deleted");
                await loadDashboard();
            } catch (err) {
                toast(err.message || "Delete failed", true);
            }
            return;
        }
        if ((action === "stop" || action === "restart") && !confirm(action === "stop" ? "Stop '" + file + "'?" : "Restart '" + file + "'?")) {
            return;
        }
        try {
            var res = await API.botAction(file, action);
            if (!res.ok) throw new Error(res.error || "Action failed");
            toast(res.message || ("Bot " + action + " requested"));
            await loadDashboard();
        } catch (err) {
            toast(err.message || "Action failed", true);
        }
    }

    function selectTab(view) {
        document.querySelectorAll(".tab").forEach(function (t) {
            t.classList.toggle("active", t.getAttribute("data-view") === view);
        });
        document.querySelectorAll(".tab-view").forEach(function (v) {
            v.classList.toggle("active", v.getAttribute("data-view") === view);
        });
        if (view === "logs" && state.detailFile) {
            loadLogs(state.detailFile);
        } else if (view === "env" && state.detailFile) {
            loadEnv(state.detailFile);
        }
    }

    function showDetail(file, view) {
        state.detailFile = file;
        els.detailTitle.textContent = file;
        els.detailPanel.hidden = false;
        els.detailPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
        var active = document.querySelector(".tab.active");
        selectTab(view || (active ? active.getAttribute("data-view") : "logs"));
    }

    async function loadLogs(file) {
        els.logBox.innerHTML = '<span class="log-empty">Loading logs...</span>';
        try {
            var res = await API.logs(file);
            var logs = res.logs || "";
            els.logHint.textContent = res.truncated ? "Showing last 64 KB (truncated)" : "";
            els.logBox.textContent = logs || "(No output yet. Start the bot to capture logs.)";
        } catch (err) {
            els.logBox.innerHTML = '<span class="log-empty">' + esc(err.message || "Failed to load logs") + "</span>";
        }
    }

    async function loadEnv(file) {
        els.envRows.innerHTML = "";
        try {
            var res = await API.envGet(file);
            var env = res.env || {};
            renderEnvRows(env);
        } catch (err) {
            toast(err.message || "Failed to load env", true);
        }
    }

    function renderEnvRows(env) {
        els.envRows.innerHTML = "";
        Object.keys(env).forEach(function (k) {
            addEnvRow(k, env[k]);
        });
        if (!Object.keys(env).length) {
            els.envRows.innerHTML = '<div class="log-empty">No environment variables yet. Add one below.</div>';
        }
    }

    function addEnvRow(key, value) {
        var row = document.createElement("div");
        row.className = "env-row";
        var keyInput = document.createElement("input");
        keyInput.className = "key-in";
        keyInput.placeholder = "KEY";
        keyInput.value = key || "";
        keyInput.spellcheck = false;
        var valInput = document.createElement("input");
        valInput.className = "val-in";
        valInput.placeholder = "value";
        valInput.value = value == null ? "" : value;
        valInput.spellcheck = false;
        var del = document.createElement("button");
        del.type = "button";
        del.className = "env-del";
        del.setAttribute("aria-label", "Remove variable");
        del.innerHTML = '<i class="ph ph-trash" aria-hidden="true"></i>';
        del.addEventListener("click", function () {
            row.remove();
        });
        row.appendChild(keyInput);
        row.appendChild(valInput);
        row.appendChild(del);
        els.envRows.appendChild(row);
    }

    async function saveEnv(file) {
        var env = {};
        var rows = els.envRows.querySelectorAll(".env-row");
        rows.forEach(function (row) {
            var k = row.querySelector(".key-in").value.trim();
            var v = row.querySelector(".val-in").value;
            if (k) env[k] = v;
        });
        try {
            var res = await API.envSet(file, env);
            var saved = document.getElementById("envSaved");
            saved.classList.add("show");
            setTimeout(function () { saved.classList.remove("show"); }, 2500);
            toast(res.message || "Environment saved");
        } catch (err) {
            toast(err.message || "Failed to save env", true);
        }
    }

    function startAutoRefresh() {
        stopAutoRefresh();
        state.refreshTimer = setInterval(loadDashboard, 30000);
        if (state.detailFile) {
            state.logTimer = setInterval(function () {
                if (document.querySelector(".tab.active").getAttribute("data-view") === "logs") {
                    loadLogs(state.detailFile);
                }
            }, 3000);
        }
    }

    function stopAutoRefresh() {
        if (state.refreshTimer) { clearInterval(state.refreshTimer); state.refreshTimer = null; }
        if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
    }

    document.getElementById("logoutBtn").addEventListener("click", function () {
        API.logout();
        location.replace("index.html");
    });

    document.getElementById("refreshBtn").addEventListener("click", loadDashboard);
    document.getElementById("refreshLogBtn").addEventListener("click", function () {
        if (state.detailFile) loadLogs(state.detailFile);
    });

    document.getElementById("autoRefresh").addEventListener("change", function (e) {
        state.autoRefresh = e.target.checked;
        if (state.autoRefresh) startAutoRefresh();
        else stopAutoRefresh();
    });

    document.getElementById("addEnvBtn").addEventListener("click", function () {
        addEnvRow("", "");
    });

    document.getElementById("saveEnvBtn").addEventListener("click", function () {
        if (state.detailFile) saveEnv(state.detailFile);
    });

    document.querySelectorAll(".tab").forEach(function (t) {
        t.addEventListener("click", function () {
            selectTab(t.getAttribute("data-view"));
        });
    });

    loadDashboard();
})();