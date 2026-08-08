import postgres from 'postgres';
import { env } from '$env/dynamic/private';

/** Matches the loopback publication of catalogue-ceramics/docker-compose.yml. */
const DEFAULT_URL = 'postgresql://catalogue:catalogue@127.0.0.1:5434/ateliera';

/** One pool for the process; SvelteKit reuses this module across requests. */
export const sql = postgres(env.DATABASE_URL || DEFAULT_URL, {
	max: 4,
	idle_timeout: 20,
	// Read-only reporting queries: a slow one should surface as an error rather
	// than hold a connection open behind the page load.
	connect_timeout: 10
});
