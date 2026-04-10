import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "agent-core onboarding",
  description: "Set up your team's Slack agent in under 5 minutes.",
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
