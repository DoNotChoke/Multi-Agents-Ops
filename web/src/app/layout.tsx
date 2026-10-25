import type { Metadata } from "next";
import { DM_Mono, Fustat } from "next/font/google";
import "./globals.css";

const sans = Fustat({ subsets: ["latin"], variable: "--font-fustat" });
const mono = DM_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-dm-mono",
});

export const metadata: Metadata = {
  title: "opsteam",
  description: "Agentic AIOps",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body>{children}</body>
    </html>
  );
}
