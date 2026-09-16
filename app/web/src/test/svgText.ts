// SPDX-License-Identifier: GPL-3.0-only

import type { Matcher } from "@testing-library/react";

// Wrapped SVG labels are split across tspans; match the enclosing <text> by
// its full text content instead of by its direct text nodes.
export function svgTextContent(label: string): Matcher {
  return (_content, node) => node?.tagName === "text" && node.textContent === label;
}
