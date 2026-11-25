import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "VideoRAG",
  description: "Local-first multimodal video knowledge retrieval",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
