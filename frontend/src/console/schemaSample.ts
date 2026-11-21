import {isMap, isScalar, isSeq, parseDocument} from 'yaml';

function fieldRange(node: unknown, path: string, prefix = ''): [number, number] | undefined {
  if (isScalar(node)) {
    return prefix === path && node.range ? [node.range[0], node.range[1]] : undefined;
  }
  const children = isMap(node)
    ? node.items.flatMap(({key, value}) => isScalar(key) && typeof key.value === 'string' ? [[key.value, value] as const] : [])
    : isSeq(node) ? node.items.map((value, index) => [String(index), value] as const) : [];
  for (const [key, value] of children) {
    const childPath = prefix ? prefix + '.' + key : key;
    if (path === childPath || path.startsWith(childPath + '.')) {
      const range = fieldRange(value, path, childPath);
      if (range) return range;
    }
  }
}

export function sampleFieldRange(json: string, path: string) {
  const document = parseDocument(json, {schema: 'json'});
  return document.errors.length ? undefined : fieldRange(document.contents, path);
}
