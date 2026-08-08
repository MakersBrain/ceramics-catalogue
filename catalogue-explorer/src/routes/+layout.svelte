<script lang="ts">
	import '../app.css';
	import favicon from '$lib/assets/favicon.svg';
	import { page } from '$app/state';

	let { children } = $props();

	let theme: 'light' | 'dark' | null = $state(null);

	// Start from the reader's own choice if they made one, otherwise from the OS,
	// so the button always offers the other theme rather than guessing.
	$effect(() => {
		if (theme === null) {
			const saved = localStorage.getItem('theme');
			theme =
				saved === 'dark' || saved === 'light'
					? saved
					: matchMedia('(prefers-color-scheme: dark)').matches
						? 'dark'
						: 'light';
			document.documentElement.setAttribute('data-theme', theme);
		}
	});

	// The toggle has to beat the OS setting in both directions, so it stamps
	// data-theme on the root element rather than flipping a class.
	function toggle() {
		theme = theme === 'dark' ? 'light' : 'dark';
		localStorage.setItem('theme', theme);
		document.documentElement.setAttribute('data-theme', theme);
	}

	/** The one page that wants the whole window rather than a column of text. */
	const sheet = $derived(page.url.pathname.startsWith('/explore'));

	function tab(active: boolean) {
		return `color: ${active ? 'var(--text-primary)' : 'var(--text-secondary)'}; background: ${
			active ? 'color-mix(in srgb, var(--accent) 12%, transparent)' : 'transparent'
		}`;
	}
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
	<title>Ceramics catalogue explorer</title>
</svelte:head>

<!--
	The shell is a fixed-height column: the nav keeps its place and everything
	below it scrolls inside `main`. That is what lets /explore hand its whole
	remaining height to the sheet, which has to know how tall it is before it can
	decide how many rows to draw.
-->
<div class="flex h-screen flex-col">
	<header class="shrink-0 border-b" style="border-color: var(--hairline)">
		<nav class="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:gap-6 sm:px-6 sm:py-4">
			<span class="text-sm font-semibold" style="color: var(--text-primary)">
				Ceramics catalogue
			</span>
			<div class="flex gap-1">
				<a href="/" class="rounded-lg px-3 py-1.5 text-sm" style={tab(page.url.pathname === '/')}>
					Overview
				</a>
				<a
					href="/explore"
					class="rounded-lg px-3 py-1.5 text-sm"
					style={tab(page.url.pathname.startsWith('/explore'))}
				>
					Explore
				</a>
				<a
					href="/compare"
					class="rounded-lg px-3 py-1.5 text-sm"
					style={tab(page.url.pathname.startsWith('/compare'))}
				>
					Compare
				</a>
				<!-- The operations section has its own layout and its own live
				     stream, so it is a link out of this shell rather than a tab
				     inside it. -->
				<a
					href="/ops"
					class="rounded-lg px-3 py-1.5 text-sm"
					style={tab(page.url.pathname.startsWith('/ops'))}
				>
					Operations
				</a>
			</div>
			<button
				type="button"
				class="ml-auto rounded-lg px-3 py-1.5 text-xs"
				style="color: var(--text-secondary); border: 1px solid var(--hairline)"
				onclick={toggle}
			>
				{theme === 'dark' ? 'Light theme' : 'Dark theme'}
			</button>
		</nav>
	</header>

	<!--
		The explore page is a spreadsheet, so it gets the viewport unpadded and
		unbounded and does its own spacing; the reading pages keep the measure that
		makes prose and charts legible.
	-->
	<main
		class="min-h-0 flex-1"
		class:overflow-hidden={sheet}
		class:mx-auto={!sheet}
		class:max-w-6xl={!sheet}
		class:overflow-y-auto={!sheet}
		class:px-4={!sheet}
		class:py-6={!sheet}
		class:sm:px-6={!sheet}
		class:sm:py-8={!sheet}
	>
		{@render children()}
	</main>
</div>
