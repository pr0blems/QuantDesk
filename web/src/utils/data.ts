import type { ApiList, ApiObject } from "../api/types";

export function asObject(value: unknown): ApiObject {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as ApiObject
    : {};
}

export function asList(value: unknown): ApiList {
  return Array.isArray(value)
    ? value.filter((item): item is ApiObject => item !== null && typeof item === "object" && !Array.isArray(item))
    : [];
}

export function stringValue(value: unknown, fallback = "--"): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

export function numberValue(value: unknown, fallback = 0): number {
  const candidate = typeof value === "number" ? value : Number(value);
  return Number.isFinite(candidate) ? candidate : fallback;
}

export function booleanValue(value: unknown): boolean {
  return value === true || value === 1 || value === "1" || value === "true";
}

export function itemList(payload: ApiObject, key = "items"): ApiList {
  return asList(payload[key]);
}

export function firstList(payload: ApiObject, ...keys: string[]): ApiList {
  for (const key of keys) {
    const list = asList(payload[key]);
    if (list.length > 0) return list;
  }
  return [];
}

export function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => stringValue(item, "")).filter(Boolean)
    : [];
}

export function compactJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "[无法序列化的数据]";
  }
}

export function parseJsonObject(value: string, label: string): Record<string, number> {
  if (!value.trim()) return {};
  const parsed: unknown = JSON.parse(value);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${label}必须是 JSON 对象`);
  }
  const result: Record<string, number> = {};
  for (const [key, item] of Object.entries(parsed)) {
    const number = Number(item);
    if (!Number.isFinite(number)) throw new Error(`${label}.${key} 必须是数字`);
    result[key] = number;
  }
  return result;
}
