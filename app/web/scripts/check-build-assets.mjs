// SPDX-License-Identifier: GPL-3.0-only

// Post-build guard: the MapLibre worker must be one self-contained chunk.
// A worker chunk with relative imports fails at runtime inside the Worker
// scope, so this fails the build instead of shipping a broken map.

import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const assetsDirectory = fileURLToPath(
  new URL("../dist/assets/", import.meta.url),
);
const assetNames = await readdir(assetsDirectory);
const scriptNames = assetNames.filter((name) => /\.(?:m?js)$/.test(name));
const workerNames = scriptNames.filter((name) =>
  /^maplibre-gl-worker-[A-Za-z0-9_-]{8}\.(?:m?js)$/.test(name),
);

if (workerNames.length !== 1) {
  throw new Error(
    `Expected one bundled MapLibre worker chunk, found ${workerNames.length}.`,
  );
}

const relativeStaticImportPattern =
  /\bimport(?:[^"'`;]*?\bfrom)?\s*["'](\.\/[^"']+)["']/g;
const relativeDynamicImportPattern =
  /\bimport\s*\(\s*["'](\.\/[^"']+)["']\s*\)/g;
const relativeFromPattern = /\bfrom\s*["'](\.\/[^"']+)["']/g;
const workerName = workerNames[0];
const workerPath = path.join(assetsDirectory, workerName);
const workerContent = await readFile(workerPath, "utf8");
const workerRelativeImports = [
  ...workerContent.matchAll(relativeStaticImportPattern),
  ...workerContent.matchAll(relativeDynamicImportPattern),
];

if (workerRelativeImports.length > 0) {
  throw new Error(
    `Bundled MapLibre worker contains relative imports: ${workerRelativeImports
      .map((match) => match[1])
      .join(", ")}`,
  );
}

for (const scriptName of scriptNames) {
  const scriptPath = path.join(assetsDirectory, scriptName);
  const content = await readFile(scriptPath, "utf8");
  for (const match of content.matchAll(relativeFromPattern)) {
    const specifier = match[1].split(/[?#]/, 1)[0];
    const resolvedPath = path.resolve(path.dirname(scriptPath), specifier);
    try {
      if (!(await stat(resolvedPath)).isFile()) throw new Error();
    } catch {
      throw new Error(
        `${scriptName} references missing build asset ${match[1]}.`,
      );
    }
  }
}

const workerSize = (await stat(workerPath)).size;
console.log(
  `Verified bundled worker ${workerName} (${workerSize} bytes): no relative imports; all emitted relative from specifiers resolve.`,
);
