import { Empty } from "@/components/states";
import { Card, PageHeader } from "@/components/ui/card";

// Signed-in only and rendered per request; see the (app) layout.
export const instant = false;

export default function OverviewPage() {
  return (
    <>
      <PageHeader title="Overview" subtitle="Last 24 hours" />
      <Card>
        <Empty
          title="No incidents yet"
          hint="Service health appears here once detection is on."
        />
      </Card>
    </>
  );
}
