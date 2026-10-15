/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // 容器化部署（deploy/docker-compose.yml 的 web 服务）：只产出运行必需的
  // 文件树到 .next/standalone，配 web/Dockerfile 多阶段构建用。
  // 本地 `npm run dev` / `npm start` 不受影响。
  output: "standalone",
  // 关闭左下角的 Next.js 开发者工具圆钮（仅开发模式出现，易与应用功能混淆）
  devIndicators: false,
  // 同源反向代理：浏览器只访问本服务，由 Next 转发到后端。
  // 其他设备通过 http://<本机IP>:3000 访问时无需任何额外配置（也避开 CORS）。
  async rewrites() {
    const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
    return [
      { source: "/api/v1/:path*", destination: `${backend}/api/v1/:path*` },
      { source: "/v1/:path*", destination: `${backend}/v1/:path*` },
      { source: "/healthz", destination: `${backend}/healthz` },
    ];
  },
};

export default nextConfig;
