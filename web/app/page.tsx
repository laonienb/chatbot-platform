import { redirect } from "next/navigation";

export default function Home() {
  // token 在 localStorage（客户端），/chat 页面加载后自行分流到登录页
  redirect("/chat");
}
