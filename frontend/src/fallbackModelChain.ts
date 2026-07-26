export const MAX_FALLBACK_TEST_MODELS = 10;

export function normalizeFallbackModelChain(
  models?: string[] | null,
  legacyModel?: string | null,
) {
  const candidates = models?.length ? models : [legacyModel || ""];
  const normalized: string[] = [];
  const seen = new Set<string>();

  for (const rawModel of candidates) {
    const model = rawModel.trim().slice(0, 160);
    if (!model || seen.has(model)) continue;
    seen.add(model);
    normalized.push(model);
    if (normalized.length >= MAX_FALLBACK_TEST_MODELS) break;
  }

  return normalized;
}

export function moveFallbackModel(models: string[], index: number, direction: -1 | 1) {
  const targetIndex = index + direction;
  if (index < 0 || index >= models.length || targetIndex < 0 || targetIndex >= models.length) {
    return models;
  }

  const reordered = [...models];
  [reordered[index], reordered[targetIndex]] = [reordered[targetIndex], reordered[index]];
  return reordered;
}

export function firstUnusedFallbackModel(availableModels: string[], selectedModels: string[]) {
  const selected = new Set(selectedModels);
  return availableModels.find((model) => model && !selected.has(model)) || null;
}
