<script lang="ts">
	import { page } from '$app/state';
	import { invalidateAll } from '$app/navigation';
	import { onMount, setContext } from 'svelte';
	import { OpsStream } from '$lib/ops/stream.svelte';
	import ConnectionBadge from '$lib/ops/ConnectionBadge.svelte';

	let { children } = $props();

	// One subscription for the whole section. Every page reads from this store;
	// only /ops/runs/[id] opens a second, narrower one, so a browser never has
	// more than two streams open against the six-per-origin cap.
	const stream = new OpsStream('workers,notifications,runs,progress');
	setContext('ops-stream', stream);

	onMount(() => {
		stream.connect(() => invalidateAll());
		return () => stream.disconnect();
	});

	const tabs = [
		{ href: '/ops', label: 'Overview' },
		{ href: '/ops/runs', label: 'Runs' },
		{ href: '/ops/sources', label: 'Sources' },
		{ href: '/ops/notifications', label: 'Notifications' },
		{ href: '/ops/metrics', label: 'Metrics' }
	];

	const current = $derived(page.url.pathname);
	const unacknowledged = $derived(stream.unacknowledged.length);
	const busy = $derived(stream.workers.filter((w) => w.status === 'busy').length);
</script>

<div class="bg-base-200/40 min-h-dvh">
	<header class="border-base-300 bg-base-100 border-b">
		<!-- The breadcrumb and the worker count are the first things to go on a
		     phone: the tabs and the connection state are what this header is for. -->
		<div class="mx-auto flex max-w-(--shell) items-center gap-3 px-3 py-2 sm:gap-4 sm:px-4 sm:py-3">
			<a href="/" class="text-base-content/60 hover:text-base-content shrink-0 text-sm">
				<span class="hidden sm:inline">catalogue</span>
				<span class="sm:hidden">&larr;</span>
			</a>
			<span class="text-base-content/30 hidden sm:inline">/</span>
			<span class="hidden font-semibold sm:inline">operations</span>

			<!-- Five tabs plus a breadcrumb and a badge do not fit until about 900px,
			     so the strip keeps its own scroll container up to `lg`. Releasing it at
			     `sm` let the header overflow its box between 640 and 900. -->
			<nav
				class="-mx-1 flex min-w-0 flex-1 gap-1 overflow-x-auto px-1 lg:ml-4 lg:flex-none [scrollbar-width:none]"
			>
				{#each tabs as tab (tab.href)}
					<a
						href={tab.href}
						class="rounded px-2.5 py-1 text-sm whitespace-nowrap transition-colors sm:px-3
						{current === tab.href || (tab.href !== '/ops' && current.startsWith(tab.href))
							? 'bg-primary text-primary-content'
							: 'hover:bg-base-200'}"
					>
						{tab.label}
						{#if tab.href === '/ops/notifications' && unacknowledged > 0}
							<span class="badge badge-warning badge-sm ml-1">{unacknowledged}</span>
						{/if}
					</a>
				{/each}
			</nav>

			<div class="ml-auto flex shrink-0 items-center gap-3 text-sm">
				<span class="text-base-content/60 hidden lg:inline">
					{busy}/{stream.workers.length} workers busy
				</span>
				<ConnectionBadge state={stream.connection} />
			</div>
		</div>
	</header>

	<main class="mx-auto max-w-(--shell) px-3 py-5 sm:px-4 sm:py-6">
		{@render children()}
	</main>
</div>
