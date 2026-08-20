/* HostBot web API client (login + dashboard) */
(function () {
    "use strict";

    var API = window.HostBotAPI || {};

    API.base = (window.HOSTBOT_API_URL || "").trim() || location.origin;

    API.getToken = function () {
        return localStorage.getItem("hostbot_token") || sessionStorage.getItem("hostbot_token") || "";
    };

    API.setToken = function (t, remember) {
        remember = !!remember;
        localStorage.removeItem("hostbot_token");
        sessionStorage.removeItem("hostbot_token");
        if (t) {
            (remember ? localStorage : sessionStorage).setItem("hostbot_token", t);
        }
        try { localStorage.setItem("hostbot_remember", remember ? "1" : "0"); } catch (e) {}
    };

    API.isRemembered = function () {
        if (localStorage.getItem("hostbot_token")) return true;
        return localStorage.getItem("hostbot_remember") === "1";
    };

    API.clearToken = function () {
        localStorage.removeItem("hostbot_token");
        sessionStorage.removeItem("hostbot_token");
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
        var res = await fetch(API.base + path, opts);
        var data = null;
        try { data = await res.json(); } catch (e) { /* no body */ }
        if (!res.ok) {
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

    API.deleteFile = function (file) {
        return API.api("/api/delete", { method: "POST", body: { file: file } });
    };

    window.HostBotAPI = API;
})();