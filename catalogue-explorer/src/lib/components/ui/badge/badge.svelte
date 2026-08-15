<script lang="ts" module>
	import { type VariantProps, tv } from 'tailwind-variants';

	export const badgeVariants = tv({
		base: 'focus-visible:ring-ring/50 group/badge inline-flex w-fit shrink-0 items-center justify-center gap-1.5 overflow-hidden rounded-sm border px-2 py-0.5 text-xs font-semibold whitespace-nowrap transition-colors focus-visible:ring-2 [&>svg]:pointer-events-none [&>svg]:size-3',
		variants: {
			variant: {
				default: 'bg-secondary text-foreground border-transparent',
				secondary: 'text-muted-foreground border-transparent bg-transparent',
				outline: 'text-foreground border-input bg-transparent',
				accent: 'bg-accent text-accent-foreground border-transparent'
			}
		},
		defaultVariants: {
			variant: 'default'
		}
	});

	export type BadgeVariant = VariantProps<typeof badgeVariants>['variant'];
</script>

<script lang="ts">
	import type { HTMLAnchorAttributes } from 'svelte/elements';
	import { cn, type WithElementRef } from '$lib/utils';

	let {
		ref = $bindable(null),
		href,
		class: className,
		variant = 'default',
		children,
		...restProps
	}: WithElementRef<HTMLAnchorAttributes> & { variant?: BadgeVariant } = $props();
</script>

<svelte:element
	this={href ? 'a' : 'span'}
	bind:this={ref}
	data-slot="badge"
	{href}
	class={cn(badgeVariants({ variant }), className)}
	{...restProps}
>
	{@render children?.()}
</svelte:element>
