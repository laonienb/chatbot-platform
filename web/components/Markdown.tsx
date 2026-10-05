"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** 聊天消息的 Markdown 渲染（GFM：列表/表格/删除线等），样式走 .md-body。 */
export default function Markdown({ content }: { content: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}
