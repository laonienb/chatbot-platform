/** 聊天记录导出：全量拉取消息 → Markdown / JSON 文件下载（纯前端）。 */

import { platformApi, type Conversation, type Message, type Persona } from "./api";

const FETCH_PAGE = 100;
const MAX_PAGES = 200; // 防御性上限：2 万条

/** 分页拉取会话的全部消息，按时间升序返回（循环 before_id 直到取完）。 */
export async function fetchAllMessages(conversationId: string): Promise<Message[]> {
  let all: Message[] = [];
  let beforeId: string | undefined;
  for (let i = 0; i < MAX_PAGES; i++) {
    const page: Message[] = await platformApi.messages(conversationId, {
      limit: FETCH_PAGE,
      ...(beforeId ? { before_id: beforeId } : {}),
    });
    all = [...page, ...all];
    if (page.length < FETCH_PAGE) break;
    if (beforeId && page[0].id === beforeId) break; // 无进展保护
    beforeId = page[0].id;
  }
  return all;
}

function fmtTime(iso: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function toMarkdown(conv: Conversation, persona: Persona | undefined, messages: Message[]): string {
  const title = conv.title ?? persona?.name ?? "会话";
  const lines: string[] = [
    `# ${title}`,
    "",
    `- 人设：${persona?.name ?? "-"}`,
    `- 消息数：${messages.length}`,
    `- 导出时间：${fmtTime(new Date().toISOString())}`,
    "",
    "---",
    "",
  ];
  const roleLabel = (m: Message) => (m.role === "user" ? "我" : m.role === "system" ? "系统" : persona?.name ?? "AI");
  for (const m of messages) {
    lines.push(`### ${roleLabel(m)} · ${fmtTime(m.created_at)}`);
    if (m.role === "assistant" && m.model) lines.push(`*模型：${m.model}*`, "");
    lines.push("", m.content, "");
  }
  return lines.join("\n");
}

export function toJson(conv: Conversation, persona: Persona | undefined, messages: Message[]): string {
  return JSON.stringify(
    {
      conversation: { id: conv.id, title: conv.title, persona: persona?.name ?? null, model: conv.model },
      exported_at: new Date().toISOString(),
      message_count: messages.length,
      messages: messages.map((m) => ({
        role: m.role,
        content: m.content,
        model: m.model,
        created_at: m.created_at,
      })),
    },
    null,
    2
  );
}

/** 触发浏览器下载（Blob + a[download]，无需后端）。 */
export function downloadText(filename: string, text: string, mime: string) {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 文件名安全化：去掉路径分隔符与控制字符，限长。 */
export function safeFilename(name: string): string {
  const cleaned = name.replace(/[\\/:*?"<>|\r\n\t]/g, "_").slice(0, 60).trim();
  return cleaned || "会话";
}
