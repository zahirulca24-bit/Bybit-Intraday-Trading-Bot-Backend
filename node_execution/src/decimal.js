export function positiveNumber(value, label) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error(`${label} must be positive`);
  return number;
}

function normalizeDecimal(value) {
  const text = String(value ?? '').trim();
  if (!/^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(text)) throw new Error(`Invalid decimal: ${text}`);
  const negative = text.startsWith('-');
  const unsigned = text.replace(/^[+-]/, '');
  const [whole = '0', fraction = ''] = unsigned.split('.');
  return { negative, whole: whole || '0', fraction };
}

function scaledInteger(value, scale) {
  const parsed = normalizeDecimal(value);
  if (parsed.fraction.length > scale) {
    const remainder = parsed.fraction.slice(scale);
    if (/[1-9]/.test(remainder)) throw new Error(`Decimal ${value} exceeds supported scale ${scale}`);
  }
  const digits = `${parsed.whole}${parsed.fraction.padEnd(scale, '0').slice(0, scale)}`.replace(/^0+(?=\d)/, '');
  const result = BigInt(digits || '0');
  return parsed.negative ? -result : result;
}

export function isStepAligned(value, step) {
  const parsedValue = normalizeDecimal(value);
  const parsedStep = normalizeDecimal(step);
  const scale = Math.max(parsedValue.fraction.length, parsedStep.fraction.length);
  const stepInt = scaledInteger(step, scale);
  if (stepInt <= 0n) return false;
  return scaledInteger(value, scale) % stepInt === 0n;
}

export function decimalGte(left, right) {
  const a = normalizeDecimal(left);
  const b = normalizeDecimal(right);
  const scale = Math.max(a.fraction.length, b.fraction.length);
  return scaledInteger(left, scale) >= scaledInteger(right, scale);
}

export function decimalLte(left, right) {
  const a = normalizeDecimal(left);
  const b = normalizeDecimal(right);
  const scale = Math.max(a.fraction.length, b.fraction.length);
  return scaledInteger(left, scale) <= scaledInteger(right, scale);
}
