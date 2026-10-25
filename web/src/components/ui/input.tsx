import type { InputHTMLAttributes } from "react";

export function Input({
  className = "",
  ...props
}: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`h-[34px] w-full rounded-md border border-[#cfd7e0] bg-white px-2.5 aria-invalid:border-crit ${className}`}
      {...props}
    />
  );
}
