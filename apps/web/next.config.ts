import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The API base is read at build time from NEXT_PUBLIC_API_BASE; mock mode needs no server.
  images: { unoptimized: true },
  // pdf.js has an optional Node-only "canvas" dependency that the browser build never needs.
  webpack: (config: { resolve: { alias: Record<string, false | string> } }) => {
    config.resolve.alias.canvas = false;
    return config;
  },
};

export default nextConfig;
