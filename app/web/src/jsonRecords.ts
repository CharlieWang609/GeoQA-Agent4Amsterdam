// SPDX-License-Identifier: GPL-3.0-only

// Loose accessors over the JSON documents the API returns for LLM-authored
// artifacts (task specifications, bindings, workflows), shared by the review
// and diagram code.

import type { JsonValue } from "./types";

export function asRecord(value: JsonValue | undefined): Record<string, JsonValue> | null {
  return value && !Array.isArray(value) && typeof value === "object" ? value : null;
}

export function recordArray(value: JsonValue | undefined): Record<string, JsonValue>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

// Like recordArray, but null when any element is not a record.
export function strictRecordArray(value: JsonValue | undefined): Record<string, JsonValue>[] | null {
  if (!Array.isArray(value)) return null;
  const records = value.filter(isRecord);
  return records.length === value.length ? records : null;
}

export function stringArray(value: JsonValue | undefined): string[] {
  if (typeof value === "string") return [value];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

export function nonBlankString(value: JsonValue | undefined): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

// "https://…#SpatialJoinCountTess" -> "Spatial join count tess",
// "zero_count_supports" -> "Zero count supports".
export function readableLabel(value: string): string {
  const terminal = value.includes("#") ? value.split("#").at(-1)! : value;
  const spaced = terminal
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim();
  return spaced ? `${spaced.charAt(0).toUpperCase()}${spaced.slice(1)}` : value;
}

export function errorMessage(caught: unknown, fallback: string): string {
  return caught instanceof Error ? caught.message : fallback;
}

function isRecord(item: JsonValue): item is Record<string, JsonValue> {
  return Boolean(item) && !Array.isArray(item) && typeof item === "object";
}
