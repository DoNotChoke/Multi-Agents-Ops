import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";

// Tokens and session data live only in the server's HttpOnly cookie.
test("no source file touches localStorage or sessionStorage", () => {
  const offenders: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name)) {
        if (
          /\b(localStorage|sessionStorage)\b/.test(readFileSync(path, "utf8"))
        )
          offenders.push(path);
      }
    }
  };
  walk(join(__dirname));
  expect(offenders).toEqual([]);
});
