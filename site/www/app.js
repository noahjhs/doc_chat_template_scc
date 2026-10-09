// Shared by the signed-in pages (signin, signup, guide). Casper's service is
// reached same-origin under /api/ (nginx proxies it), so there's no CORS.
// The session token lives in localStorage: one sign-in per browser.
const Casper = {
  PAIR_SCHEME: "${PAIR_SCHEME}",
  DOWNLOAD_URL: "${DOWNLOAD_URL}",

  session() {
    const token = localStorage.getItem("casper_token");
    return token ? { token, username: localStorage.getItem("casper_username") } : null;
  },
  saveSession(r) {
    localStorage.setItem("casper_token", r.token);
    localStorage.setItem("casper_username", r.username);
  },
  signOut() {
    localStorage.removeItem("casper_token");
    localStorage.removeItem("casper_username");
  },

  async api(path, { method = "GET", body } = {}) {
    const s = Casper.session();
    const headers = { "Content-Type": "application/json" };
    if (s) headers.Authorization = "Bearer " + s.token;
    let r;
    try {
      r = await fetch("/api" + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    } catch (e) {
      throw new Error("Couldn't reach Casper. Try again in a moment.");
    }
    const data = await r.json().catch(() => ({}));
    if (r.status === 401 && s) { Casper.signOut(); }
    if (!r.ok) throw Object.assign(new Error(data.detail || "Something went wrong."), { status: r.status });
    return data;
  },

  async connectedHosts() {
    try {
      const d = await Casper.api("/hosts");
      return new Set(d.hosts.filter(h => h.connected).map(h => h.host_id));
    } catch (e) { return null; }
  },

  // Hands this browser's session to the Casper app on this Mac (it registers
  // the casper:// URL scheme), which connects the Mac to the account.
  pair() {
    const s = Casper.session();
    const a = document.createElement("a");
    a.href = `${Casper.PAIR_SCHEME}://pair?token=${encodeURIComponent(s.token)}&username=${encodeURIComponent(s.username)}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  },

  // Resolves true once a host connects that wasn't connected before.
  async waitForNewHost(before, seconds = 30) {
    for (let i = 0; i < seconds / 1.5; i++) {
      await new Promise(r => setTimeout(r, 1500));
      const now = await Casper.connectedHosts();
      if (now && [...now].some(id => !before.has(id))) return true;
    }
    return false;
  },

  // Plain text in, safe HTML out: escapes everything, then allows **bold**,
  // `code`, links, and line breaks. The download link becomes a button.
  render(text) {
    const esc = s => s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
    let h = esc(text);
    h = h.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
    h = h.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (m, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
    h = h.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (m, pre, url) => {
      const clean = url.replace(/[.,;:]+$/, ""), tail = url.slice(clean.length);
      if (clean === Casper.DOWNLOAD_URL) return `${pre}<a class="btn inline" href="${clean}">Download Casper</a>${tail}`;
      return `${pre}<a href="${clean}" target="_blank" rel="noopener">${clean}</a>${tail}`;
    });
    return h.replace(/\n/g, "<br>");
  },

  param(name) { return new URLSearchParams(location.search).get(name); },
};
