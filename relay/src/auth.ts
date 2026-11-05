// Bearer-token checks. Both values are hashed first so the comparison runs in
// constant time and does not leak the token length.

const encoder = new TextEncoder();

async function digest(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", encoder.encode(value)));
}

export async function safeEqual(a: string, b: string): Promise<boolean> {
  const [ha, hb] = await Promise.all([digest(a), digest(b)]);
  let diff = 0;
  for (let i = 0; i < ha.length; i++) diff |= ha[i] ^ hb[i];
  return diff === 0;
}

export function bearerToken(request: Request): string | null {
  const header = request.headers.get("Authorization");
  if (!header) return null;
  const match = /^Bearer\s+(\S+)\s*$/i.exec(header); // scheme is case-insensitive (RFC 6750)
  return match ? match[1] : null;
}

// Fails closed: an unset or very short secret never authenticates anyone.
export async function isAuthorised(request: Request, secret: string | undefined): Promise<boolean> {
  if (!secret || secret.length < 32) return false;
  const token = bearerToken(request);
  if (token === null) return false;
  return safeEqual(token, secret);
}
