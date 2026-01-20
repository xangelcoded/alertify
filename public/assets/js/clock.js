function startClock() {
  const clock = document.getElementById("clock-text");
  if (!clock) return;

  function tick() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, "0");
    const m = String(now.getMinutes()).padStart(2, "0");
    const s = String(now.getSeconds()).padStart(2, "0");
    clock.textContent = `SYSTEM TIME: ${h}:${m}:${s}`;
  }

  tick();
  setInterval(tick, 1000);
}

document.addEventListener("DOMContentLoaded", () => {
  startClock();
});
