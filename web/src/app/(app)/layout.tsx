import { Shell } from "@/components/shell";
import { Providers } from "../providers";

export default function AppLayout({ children }: LayoutProps<"/">) {
  return (
    <Providers>
      <Shell>{children}</Shell>
    </Providers>
  );
}
