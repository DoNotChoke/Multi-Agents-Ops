import { IntegrationsScreen } from "@/features/integrations/integrations-screen";

// Signed-in only and rendered per request; see the (app) layout.
export const instant = false;

export default function IntegrationsPage() {
  return <IntegrationsScreen />;
}
