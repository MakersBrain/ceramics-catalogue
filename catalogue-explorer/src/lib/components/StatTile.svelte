<script lang="ts">
	let {
		label,
		value,
		note,
		hero = false
	}: { label: string; value: string; note?: string; hero?: boolean } = $props();
</script>

<div class="viz-surface rounded-xl px-5 py-4">
	<div class="text-xs" style="color: var(--text-secondary)">{label}</div>
	<div
		class="mt-1 font-semibold {hero ? 'text-5xl' : 'text-2xl'}"
		style="color: var(--text-primary)"
	>
		{value}
	</div>
	{#if note}
		<div class="mt-1 text-xs" style="color: var(--text-muted)">{note}</div>
	{/if}
</div>
