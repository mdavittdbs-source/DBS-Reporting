// Light, dark, or match the system, remembered in this browser. Loaded in <head> on both pages so
// the saved choice applies before anything is drawn (no flash of the wrong theme).
(() => {
  const KEY = "david-theme";
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const listeners = [];

  function read() {
    try { return localStorage.getItem(KEY) || "system"; } catch { return "system"; }
  }
  function effective() {
    const choice = read();
    return choice === "system" ? (systemDark.matches ? "dark" : "light") : choice;
  }
  // The dark version of the logo is a <source> picked by a media query; point it at the choice.
  function syncLogos() {
    const choice = read();
    const media = choice === "dark" ? "all" : choice === "light" ? "not all" : "(prefers-color-scheme: dark)";
    document.querySelectorAll("source[data-dark]").forEach((s) => { s.media = media; });
  }
  function apply() {
    const choice = read();
    if (choice === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = choice;
    syncLogos();
    listeners.forEach((fn) => fn(effective()));
  }

  window.davidTheme = {
    get: read,
    effective,
    set(choice) {
      try {
        if (choice === "system") localStorage.removeItem(KEY);
        else localStorage.setItem(KEY, choice);
      } catch {}
      apply();
    },
    onChange(fn) { listeners.push(fn); },
  };
  systemDark.addEventListener("change", () => { if (read() === "system") listeners.forEach((fn) => fn(effective())); });
  apply();
  document.addEventListener("DOMContentLoaded", syncLogos);
})();
