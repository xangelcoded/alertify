async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.error || `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("user-login-form");
  const nameEl = document.getElementById("name");
  const errEl = document.getElementById("error");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errEl.style.display = "none";

    const name = (nameEl.value || "").trim();
    if (!name) {
      errEl.textContent = "Please enter your name.";
      errEl.style.display = "block";
      return;
    }

    try {
      await postJSON("/api/auth/user-login", { name });
      window.location.href = "/user_feed.html";
    } catch (err) {
      errEl.textContent = err.message || "Login failed.";
      errEl.style.display = "block";
    }
  });
});
