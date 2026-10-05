"use client";

import { useEffect, useState } from "react";

/**
 * 头像：三层降级 —— 图片 URL → emoji（直接存在 avatar_url 里的非 URL 字符串）→ 首字母渐变。
 * 渐变颜色由名字哈希决定，同名同色、异名大概率异色。
 */

function hashName(name: string): number {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  return h;
}

const GRADIENTS = [
  "linear-gradient(135deg, #6474f0, #9a6cf5)",
  "linear-gradient(135deg, #e8618c, #f08a5d)",
  "linear-gradient(135deg, #22b8a6, #4e8fe8)",
  "linear-gradient(135deg, #f0784a, #e8b64e)",
  "linear-gradient(135deg, #7a5cf0, #4ecf8e)",
  "linear-gradient(135deg, #4a8af0, #22c1dc)",
  "linear-gradient(135deg, #e85d5d, #e85dcf)",
  "linear-gradient(135deg, #5d9ce8, #5de8c0)",
];

function isImageUrl(s: string): boolean {
  return /^https?:\/\//.test(s) || s.startsWith("data:image/");
}

export default function Avatar({
  name,
  url,
  size = 36,
  radius,
  className,
}: {
  name: string;
  url?: string | null;
  size?: number;
  radius?: number;
  className?: string;
}) {
  const [err, setErr] = useState(false);
  useEffect(() => setErr(false), [url]);
  const style: React.CSSProperties = {
    width: size,
    height: size,
    minWidth: size,
    borderRadius: radius ?? Math.max(8, Math.round(size * 0.32)),
    fontSize: url && !isImageUrl(url) && url.length <= 4 ? Math.round(size * 0.52) : Math.max(11, Math.round(size * 0.36)),
    ...(url && !isImageUrl(url)
      ? { background: "rgba(120, 140, 255, 0.12)" }
      : { background: GRADIENTS[hashName(name) % GRADIENTS.length] }),
  };

  const initial = name?.trim()?.[0]?.toUpperCase() ?? "?";

  return (
    <span className={`avatar ${className ?? ""}`} style={style}>
      {url && isImageUrl(url) && !err ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt={name} onError={() => setErr(true)} className="avatar-img" />
      ) : url && !isImageUrl(url) ? (
        url
      ) : (
        initial
      )}
    </span>
  );
}
