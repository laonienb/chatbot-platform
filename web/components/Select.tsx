"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type SelectOption = { value: string; label: string };

/**
 * 自绘下拉选择：替代原生 <select>。
 * 原生展开面板由操作系统绘制（Windows 直角白底），无法用 CSS 定制；
 * 本组件用按钮 + fixed 定位面板自绘，展开风格与主题一致。
 */
export default function Select({
  value,
  onChange,
  options,
  className,
  ariaLabel,
  title,
}: {
  value: string;
  onChange: (v: string) => void;
  options: SelectOption[];
  className?: string;
  ariaLabel?: string;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const [dropUp, setDropUp] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number; width: number } | null>(null);
  const [active, setActive] = useState(0);
  const btnRef = useRef<HTMLButtonElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const selectedIdx = options.findIndex((o) => o.value === value);
  const selected = selectedIdx >= 0 ? options[selectedIdx] : undefined;

  function openMenu() {
    const r = btnRef.current!.getBoundingClientRect();
    const est = Math.min(options.length, 8) * 36 + 12;
    const up = r.bottom + est > window.innerHeight && r.top - est > 8;
    setDropUp(up);
    setPos({ left: r.left, top: up ? r.top : r.bottom, width: r.width });
    setActive(selectedIdx >= 0 ? selectedIdx : 0);
    setOpen(true);
  }

  function choose(v: string) {
    onChange(v);
    setOpen(false);
    btnRef.current?.focus();
  }

  function onBtnKeyDown(e: React.KeyboardEvent) {
    if (!open && (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      openMenu();
    } else if (open && e.key === "Escape") {
      setOpen(false);
    }
  }

  // 打开期间：点击外部 / 滚动 / 缩放 / Esc 关闭（fixed 定位不跟随文档流）
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target;
      // 面板经 portal 挂在 body 上，也算"内部"
      if (t instanceof Element && t.closest(".select-pop")) return;
      if (!rootRef.current?.contains(t as Node)) setOpen(false);
    };
    const close = () => setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("mousedown", onDown);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} style={{ position: "relative", minWidth: 0 }}>
      <button
        ref={btnRef}
        type="button"
        className={`select-trigger${className ? ` ${className}` : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        title={title}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onBtnKeyDown}
      >
        <span className="select-trigger-label">{selected?.label ?? options[0]?.label ?? ""}</span>
      </button>
      {open &&
        pos &&
        createPortal(
          <ul
            className={`select-pop${dropUp ? " up" : ""}`}
            role="listbox"
            style={{
              left: pos.left,
              width: Math.max(pos.width, 140),
              ...(dropUp ? { bottom: window.innerHeight - pos.top } : { top: pos.top }),
            }}
          >
            {options.map((o, i) => (
              <li
                key={o.value}
                role="option"
                aria-selected={o.value === value}
                className={`select-opt${i === active ? " active" : ""}${o.value === value ? " selected" : ""}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => choose(o.value)}
              >
                {o.label}
              </li>
            ))}
          </ul>,
          document.body
        )}
    </div>
  );
}
