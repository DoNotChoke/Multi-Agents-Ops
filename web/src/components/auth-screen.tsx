import { redirect } from "next/navigation";
import { connection } from "next/server";
import { auth, signIn } from "@/auth";
import { Logo } from "./shell";

const IDP_LABELS: Record<string, string> = {
  google: "Google",
  github: "GitHub",
  microsoft: "Microsoft",
};

// Social buttons appear only for identity providers configured in Keycloak.
const IDPS = (process.env.OPSTEAM_SOCIAL_IDPS ?? "")
  .split(",")
  .map((s) => s.trim())
  .filter((s) => s in IDP_LABELS);

async function start(form: FormData) {
  "use server";
  const params: Record<string, string> = {};
  const idp = String(form.get("idp") ?? "");
  const email = String(form.get("email") ?? "").trim();
  if (idp in IDP_LABELS) params.kc_idp_hint = idp;
  if (email) params.login_hint = email;
  if (form.get("mode") === "signup") params.prompt = "create";
  await signIn("keycloak", { redirectTo: "/" }, params);
}

export async function AuthScreen({ mode }: { mode: "signin" | "signup" }) {
  // auth() draws random values, which Next refuses while prerendering; render per request instead.
  await connection();
  if (await auth()) redirect("/");
  const signup = mode === "signup";
  return (
    <div className="grid min-h-screen grid-cols-1 bg-white md:grid-cols-2">
      <div className="grid place-items-center p-10">
        <div className="w-full max-w-[400px] text-center">
          <div className="mb-4 flex justify-center">
            <Logo size="lg" />
          </div>
          <h1 className="mb-[22px] text-2xl font-semibold">
            {signup ? "Create your account" : "Welcome to opsteam"}
          </h1>
          <div className="grid gap-3 rounded-xl border border-line p-[26px]">
            {IDPS.map((idp) => (
              <form key={idp} action={start}>
                <input type="hidden" name="idp" value={idp} />
                <input type="hidden" name="mode" value={mode} />
                <button
                  type="submit"
                  className="h-10 w-full rounded-lg border border-line font-medium hover:border-[#c9d2dc]"
                >
                  Continue with {IDP_LABELS[idp]}
                </button>
              </form>
            ))}
            {IDPS.length > 0 && (
              <div className="my-1 flex items-center gap-2.5 text-xs text-muted before:h-px before:flex-1 before:bg-line after:h-px after:flex-1 after:bg-line">
                or
              </div>
            )}
            <form action={start} className="grid gap-3">
              <input type="hidden" name="mode" value={mode} />
              <input
                type="email"
                name="email"
                placeholder="Work email"
                aria-label="Work email"
                autoComplete="email"
                className="h-10 w-full rounded-lg border border-[#cfd7e0] px-2.5"
              />
              <button
                type="submit"
                className="h-10 w-full rounded-lg bg-brand-700 font-medium text-white hover:bg-brand-800"
              >
                Continue with email
              </button>
            </form>
          </div>
          <p className="mt-[18px] text-[13px] text-muted">
            {signup ? "Already have an account? " : "Don't have an account? "}
            <a
              href={signup ? "/sign-in" : "/sign-up"}
              className="text-brand-700 hover:underline"
            >
              {signup ? "Sign in" : "Sign up"}
            </a>
          </p>
        </div>
      </div>
      <div className="halftone relative hidden place-items-center overflow-hidden bg-brand-700 md:grid">
        <p className="relative z-10 max-w-[440px] text-center text-[28px] leading-tight font-semibold tracking-tight text-white">
          Incidents investigated with evidence, not guesses.
        </p>
      </div>
    </div>
  );
}
