import { IncidentScreen } from "@/features/incidents/incident-screen";

// Signed-in only and rendered per request; see the (app) layout.
export const instant = false;

export default async function IncidentPage({
  params,
}: PageProps<"/incidents/[incidentId]">) {
  const { incidentId } = await params;
  return <IncidentScreen incidentId={incidentId} />;
}
