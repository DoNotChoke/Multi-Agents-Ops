import { ServicesScreen } from "@/features/catalog/services-screen";

// Signed-in only and rendered per request; see the (app) layout.
export const instant = false;

export default function ServicesPage() {
  return <ServicesScreen />;
}
