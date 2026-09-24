const $ = selector => document.querySelector(selector);
try { $("#loginForm [name=username]").value = sessionStorage.getItem("subvault:last-username") || "admin"; } catch { /* Storage may be disabled. */ }

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.className = "toast show error";
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => { toast.className = "toast"; }, 3000);
}

$("#loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#loginButton");
  const payload = Object.fromEntries(new FormData(form));
  button.disabled = true;
  button.textContent = "正在验证…";
  try {
    const response = await fetch("/vault/api/login", {
      method: "POST",
      cache: "no-store",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({error: "服务暂时不可用"}));
    if (!response.ok) throw new Error(data.error || `登录失败 (${response.status})`);
    form.reset();
    location.reload();
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
    button.textContent = "进入控制台";
  }
});
