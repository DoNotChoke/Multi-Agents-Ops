import { Empty } from "@/components/states";
import { Card, PageHeader } from "@/components/ui/card";

// TODO(T-1.8): replace with the real screen in slice F2.
export default function IntegrationsPage() {
  return (
    <>
      <PageHeader
        title="Integrations"
        subtitle="Data sources agents can read"
      />
      <Card>
        <Empty title="Coming next" />
      </Card>
    </>
  );
}
