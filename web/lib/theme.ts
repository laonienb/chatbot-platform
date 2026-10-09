/** 主题管理：localStorage 持久化 + <html data-theme> 应用（首屏由 layout 内联脚本引导）。 */

export type Theme = "dark" | "light";

const KEY = "cp_theme";

export function getTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* 隐私模式等场景忽略 */
  }
}
