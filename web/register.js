/* HostBot registration page logic */
(function () {
    "use strict";

    var form = document.getElementById("regForm");
    var errorBox = document.getElementById("authError");
    var errorText = document.getElementById("authErrorText");
    var btn = document.getElementById("regBtn");
    var btnLabel = btn.querySelector("span");
    var planOptions = document.getElementById("planOptions");

    function showError(msg) {
        errorText.textContent = msg;
        errorBox.classList.add("show");
    }

    function hideError() {
        errorBox.classList.remove("show");
    }

    document.querySelectorAll(".eye-btn").forEach(function (bt) {
        bt.addEventListener("click", function () {
            var input = document.getElementById(bt.getAttribute("data-eyes"));
            var show = input.type === "password";
            input.type = show ? "text" : "password";
            bt.querySelector("i").className = show ? "ph ph-eye-slash" : "ph ph-eye";
            bt.setAttribute("aria-label", show ? "Hide password" : "Show password");
            input.focus();
        });
    });

    planOptions.addEventListener("change", function (e) {
        if (e.target.name === "plan") {
            planOptions.querySelectorAll(".plan-opt").forEach(function (opt) {
                opt.classList.toggle("selected", opt.getAttribute("data-plan") === e.target.value);
            });
        }
    });

    form.querySelectorAll("input").forEach(function (i) {
        i.addEventListener("input", hideError);
    });

    form.addEventListener("submit", async function (e) {
        e.preventDefault();
        hideError();

        var username = document.getElementById("username").value.trim();
        var telegramId = document.getElementById("telegramId").value.trim();
        var password = document.getElementById("password").value;
        var confirm = document.getElementById("confirm").value;
        var plan = (document.querySelector('input[name="plan"]:checked') || {}).value || "free";

        if (!/^[A-Za-z0-9_]{3,24}$/.test(username)) {
            showError("Username must be 3-24 characters (letters, digits, underscore).");
            return;
        }
        if (!telegramId || !/^\d+$/.test(telegramId)) {
            showError("Please enter your numeric Telegram ID.");
            return;
        }
        if (password.length < 6) {
            showError("Password must be at least 6 characters.");
            return;
        }
        if (password !== confirm) {
            showError("Passwords do not match.");
            return;
        }

        btn.disabled = true;
        btnLabel.textContent = "Creating account...";
        btn.innerHTML = '<i class="ph ph-circle-notch spinning" aria-hidden="true"></i><span>Creating account...</span>';
        btnLabel = btn.querySelector("span");

        try {
            var res = await window.HostBotAPI.api("/api/register", {
                method: "POST",
                body: {
                    username: username,
                    password: password,
                    telegram_id: parseInt(telegramId, 10),
                    plan: plan
                }
            });
            location.replace("login.html?registered=1");
        } catch (err) {
            showError(err.message || "Registration failed. Please try again.");
            btn.disabled = false;
            btnLabel.textContent = "Create account";
            btn.innerHTML = '<i class="ph ph-user-plus" aria-hidden="true"></i><span>Create account</span>';
            btnLabel = btn.querySelector("span");
        }
    });
})();