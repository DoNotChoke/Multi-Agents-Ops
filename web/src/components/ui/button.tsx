import type { ButtonHTMLAttributes } from "react";

const VARIANTS = {
  default: "border-line bg-white hover:border-[#c9d2dc]",
  primary: "border-brand-700 bg-brand-700 text-white hover:bg-brand-800",
  ghost: "border-transparent bg-transparent text-brand-700 hover:bg-brand-50",
} as const;

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: keyof typeof VARIANTS;
};

export function Button({
  variant = "default",
  className = "",
  type = "button",
  ...props
}: Props) {
  return (
    <button
      type={type}
      className={`inline-flex h-8 items-center gap-1.5 rounded-md border px-3 font-medium whitespace-nowrap disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  );
}
