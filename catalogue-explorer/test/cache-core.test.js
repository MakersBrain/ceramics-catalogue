// @ts-nocheck -- executed by Node's built-in test runner, outside the browser bundle.
import assert from 'node:assert/strict';
import test from 'node:test';

import { createStableCache } from '../src/lib/server/cache-core.js';

test('promotion generation invalidates a stable aggregate', async () => {
	let now = 10_000;
	let generation = 1;
	let builds = 0;
	const cache = createStableCache(async () => generation, {
		ttlMs: 60_000,
		pollMs: 5_000,
		now: () => now
	});
	const build = async () => ++builds;

	assert.equal(await cache.stable('facets', build), 1);
	assert.equal(await cache.stable('facets', build), 1);
	generation = 2;
	now += 5_001;
	assert.equal(await cache.stable('facets', build), 2);
});

test('clear invalidates entries and the polled generation', async () => {
	let reads = 0;
	let builds = 0;
	const cache = createStableCache(async () => ++reads, { now: () => 10_000 });
	assert.equal(await cache.stable('home', async () => ++builds), 1);
	cache.clear();
	assert.equal(await cache.stable('home', async () => ++builds), 2);
	assert.equal(reads, 2);
});
