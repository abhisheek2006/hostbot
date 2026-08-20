/* HostBot login page logic */
(function () {
    "use strict";

    if (window.HostBotAPI.getToken()) {
        location.replace("dashboard.html");
        return;
    }

    if (new URLSearchParams(location.search).get("registered") === "1") {
        document.getElementById("regOk").classList.add("show");
    }

    var form = document.getElementById("loginForm");
    var errorBox = document.getElementById("authError");
    var errorText = document.getElementById("authErrorText");
    var btn = document.getElementById("loginBtn");
    var btnLabel = btn.querySelector("span");
    var eyeToggle = document.getElementById("eyeToggle");
    var eyeIcon = document.getElementById("eyeIcon");
    var passwordInput = document.getElementById("password");
    var rememberMe = document.getElementById("rememberMe");

    rememberMe.checked = window.HostBotAPI.isRemembered();

    function showError(msg) {
        errorText.textContent = msg;
        errorBox.classList.add("show");
    }

    function hideError() {
        errorBox.classList.remove("show");
    }

    eyeToggle.addEventListener("click", function () {
        var show = passwordInput.type === "password";
        passwordInput.type = show ? "text" : "password";
        eyeIcon.className = show ? "ph ph-eye-slash" : "ph ph-eye";
        eyeToggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
        passwordInput.focus();
    });

    passwordInput.addEventListener("input", hideError);
    document.getElementById("username").addEventListener("input", hideError);

    form.addEventListener("submit", async function (e) {
        e.preventDefault();
        hideError();

        var username = document.getElementById("username").value.trim();
        var password = passwordInput.value;

        if (!username || !password) {
            showError("Please enter both username and password.");
            return;
        }

        btn.disabled = true;
        btnLabel.textContent = "Logging in...";
        btn.innerHTML = '<i class="ph ph-circle-notch spinning" aria-hidden="true"></i><span>Logging in...</span>';
        btnLabel = btn.querySelector("span");

        try {
            var res = await window.HostBotAPI.login(username, password);
            window.HostBotAPI.setToken(res.token, rememberMe.checked);
            location.replace("dashboard.html");
        } catch (err) {
            showError(err.message || "Login failed. Please try again.");
            btn.disabled = false;
            btnLabel.textContent = "Log in";
            btn.innerHTML = '<i class="ph ph-sign-in" aria-hidden="true"></i><span>Log in</span>';
            btnLabel = btn.querySelector("span");
        }
    });
})();