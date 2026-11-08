// The start page's search and the review buttons. Everything else is drawn by the server.
(() => {
  const filter = document.getElementById("filter");
  if (filter) filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    document.querySelectorAll("[data-search]").forEach((c) => { c.hidden = !c.dataset.search.includes(q); });
  });
  document.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.confirm && !confirm(b.dataset.confirm)) return;
    const box = b.closest(".actions");
    const r = await fetch(`/api/skills/${box.dataset.skill}/${b.dataset.action}`, {
      method: "POST",
      headers: { "X-Requested-With": "agentskills", "Content-Type": "application/json" },
      body: JSON.stringify({ version: Number(box.dataset.version), note: box.querySelector(".note").value }),
    });
    if (r.ok) location.reload();
    else alert((await r.json().catch(() => ({}))).error || "That did not work.");
  }));
})();
