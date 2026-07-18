import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Agent onboarding",
  description: "Connect a multi-tenant AI SRE to your Slack workspace.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-white text-[color:var(--foreground)]">
        {children}
      </body>
    </html>
  );
}
