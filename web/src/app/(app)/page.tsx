import { OverviewScreen } from "@/features/incidents/overview-screen";

// Signed-in only and rendered per request; see the (app) layout.
export const instant = false;

export default function OverviewPage() {
  return <OverviewScreen />;
}
