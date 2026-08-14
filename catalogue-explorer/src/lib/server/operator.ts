import { dev } from '$app/environment';
import { env } from '$env/dynamic/private';
import { createPrivateKey, randomUUID, sign } from 'node:crypto';
import { readFileSync } from 'node:fs';

export type OperatorRole = 'viewer' | 'admin';
export interface Operator {
	id: string;
	role: OperatorRole;
	authTime: number | null;
}

const members = (value: string | undefined) =>
	new Set(
		(value ?? '')
			.split(',')
			.map((item) => item.trim().toLowerCase())
			.filter(Boolean)
	);

export function operatorFromRequest(request: Request): Operator | null {
	const header = (env.CATALOGUE_OPERATOR_ID_HEADER || 'cf-access-authenticated-user-email').toLowerCase();
	const identity = (request.headers.get(header) || (dev ? env.CATALOGUE_OPERATOR_DEV_ID : '') || '')
		.trim()
		.toLowerCase();
	if (!identity) return null;
	const rawAuthTime = request.headers.get(env.CATALOGUE_OPERATOR_AUTH_TIME_HEADER || 'x-catalogue-auth-time');
	const authTime = rawAuthTime && /^\d+$/.test(rawAuthTime) ? Number(rawAuthTime) : null;
	if (members(env.CATALOGUE_OPERATOR_ADMINS).has(identity)) return { id: identity, role: 'admin', authTime };
	if (members(env.CATALOGUE_OPERATOR_VIEWERS).has(identity)) return { id: identity, role: 'viewer', authTime };
	return null;
}

export function requireSameOrigin(request: Request, url: URL): void {
	const origin = request.headers.get('origin');
	if (!origin || origin !== url.origin) throw new Error('cross-origin operator action rejected');
}

export function actorHeaders(operator: Operator, method: string, path: string): Record<string, string> {
	const privateKeyFile = env.CATALOGUE_OPERATOR_ASSERTION_PRIVATE_KEY_FILE;
	if (!privateKeyFile) throw new Error('operator assertion private key is not configured');
	const now = Math.floor(Date.now() / 1000);
	const claims = {
		kid: env.CATALOGUE_OPERATOR_ASSERTION_KEY_ID || 'current',
		sub: operator.id,
		role: operator.role,
		aud: 'catalogue-control',
		iat: now,
		exp: now + 45,
		nonce: randomUUID(),
		method: method.toUpperCase(),
		path,
		auth_time: operator.authTime
	};
	const encoded = Buffer.from(JSON.stringify(claims)).toString('base64url');
	const key = createPrivateKey(readFileSync(privateKeyFile));
	const signature = sign(null, Buffer.from(encoded, 'base64url'), key).toString('base64url');
	return {
		'x-catalogue-actor': encoded,
		'x-catalogue-actor-signature': signature
	};
}
