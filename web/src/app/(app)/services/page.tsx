import { Empty } from "@/components/states";
import { Card, PageHeader } from "@/components/ui/card";

// TODO(T-1.8): replace with the real screen in slice F2.
export default function ServicesPage() {
  return (
    <>
      <PageHeader
        title="Services"
        subtitle="Agents can only query services listed here"
      />
      <Card>
        <Empty title="Coming next" />
      </Card>
    </>
  );
}
