document.getElementById("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("error");
  const btn = document.getElementById("loginBtn");
  const btnText = btn.querySelector(".btn-text");
  const btnLoader = btn.querySelector(".btn-loader");

  errorEl.classList.add("hidden");
  btn.disabled = true;
  btnText.classList.add("hidden");
  btnLoader.classList.remove("hidden");

  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value;

  try {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      credentials: "include",
    });
    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.detail || "Invalid username or password";
      errorEl.classList.remove("hidden");
      btn.disabled = false;
      btnText.classList.remove("hidden");
      btnLoader.classList.add("hidden");
      return;
    }
    localStorage.setItem("csrf_token", data.csrf_token || "");
    localStorage.setItem("access_token", data.access_token || "");
    localStorage.setItem("admin_role", data.role);
    localStorage.setItem("admin_username", data.username);
    window.location.href = "/admin";
  } catch (err) {
    errorEl.textContent = "Network error. Please try again.";
    errorEl.classList.remove("hidden");
    btn.disabled = false;
    btnText.classList.remove("hidden");
    btnLoader.classList.add("hidden");
  }
});
