import { IncidentsScreen } from "@/features/incidents/incidents-screen";

// Signed-in only and rendered per request; see the (app) layout.
export const instant = false;

export default function IncidentsPage() {
  return <IncidentsScreen />;
}
