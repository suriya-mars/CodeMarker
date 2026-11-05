// The switchable themes. Every colour lives as a token in styles.css under
// [data-theme]; this list is what the picker shows. The canvas graph cannot read
// CSS variables by itself, so readTokens() hands it the active values.
export const THEMES = [
  { id: "deep-core", name: "Deep Core", note: "Black, blue, cyan", swatches: ["#050a12", "#1069e0", "#35d6e6"] },
  { id: "blossom", name: "Blossom", note: "Blue, white, light pink", swatches: ["#ffffff", "#1747c8", "#f7b6d2"] },
  { id: "cobalt", name: "Cobalt", note: "Saturated blue, white, ice", swatches: ["#0a2472", "#ffffff", "#7df3ff"] },
  { id: "aurora", name: "Aurora", note: "Black, emerald, violet", swatches: ["#06110f", "#0b7f69", "#c58bff"] },
  { id: "ember", name: "Ember", note: "Charcoal, rust, amber", swatches: ["#100b09", "#c2410c", "#ffb454"] },
  { id: "daylight", name: "Daylight", note: "White, blue, teal", swatches: ["#f6f9fc", "#0b62d6", "#007a8a"] },
];

const KEY = "repodigger.theme";

// A remembered theme is a convenience: if storage is blocked, the default applies.
export function loadTheme() {
  try {
    const saved = window.localStorage.getItem(KEY);
    if (THEMES.some((t) => t.id === saved)) return saved;
  } catch {
    // storage unavailable
  }
  return THEMES[0].id;
}

export function applyTheme(id) {
  document.documentElement.dataset.theme = id;
  try {
    window.localStorage.setItem(KEY, id);
  } catch {
    // storage unavailable
  }
}

export function readTokens() {
  const css = getComputedStyle(document.documentElement);
  const read = (name) => css.getPropertyValue(`--${name}`).trim();
  return {
    text: read("muted"),
    body: read("text"),
    edge: read("edge"),
    contains: read("edge-soft"),
    live: read("live"),
    via: read("via"),
    bg: read("bg"),
    kinds: {
      file: read("k-file"),
      class: read("k-class"),
      function: read("k-function"),
      method: read("k-method"),
      page: read("k-page"),
      interface: read("k-interface"),
    },
  };
}
