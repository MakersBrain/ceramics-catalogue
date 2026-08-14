"""Derived ceramics fields, as modules a source opts into.

A record holds two kinds of field, and they earn belief in different ways.
What the shop published — name, price, stock, images, its own specification
table — is *extraction*: it is either read correctly or it is a bug. What is
inferred from that text — that a product is a glaze, that it fires to cone 6,
that it is sold in 473 ml, that it is brushed on in three coats — is
*enrichment*, and it is only meaningful for a catalogue of ceramic materials.

Running enrichment over a shop that sells finished pots is not merely useless,
it invents facts: a French potter's mug came out of the SumUp scraper with a
colour read off its title and an application method of "pouring", because the
word "carafe" is what a glaze description uses for how to apply it.

So enrichment is opt-in. A source names the modules it wants in `sources.json`:

    "enrichments": ["ceramic-materials"]        the bundle every supplier uses
    "enrichments": ["classification", "firing"] or just the parts that apply

and a source that names none gets none — every derived field on its rows is
null, and only what the shop actually published survives. The field set is the
same either way, because the record contract does not change with the source.

Adding a module — a clay-body reader for grog and shrinkage, a raw-materials
one for mesh and analysis — means writing the parser in `domain`, listing it
here with the fields it owns, and adding it to the sources that publish it.
Nothing else in the pipeline needs to know it exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import domain


@dataclass(frozen=True)
class Context:
    """The published text one product's enrichment is derived from.

    `identity` and `corpus` are separate on purpose and the distinction is the
    reason classification works at all: marketing prose names every property a
    product does *not* have ("unlike glazes", "apply a clear glaze on top"), so
    what a product *is* may only be read from its title, its department and its
    specification table. A firing range or a safety claim carries its own
    evidence, so those read the description too.
    """

    name: str = ""
    description: str = ""
    categories: tuple[str, ...] = ()
    specification: str = ""
    colour_hint: str | None = None

    @property
    def category_text(self) -> str:
        return " ".join(domain.clean(value) for value in self.categories)

    @property
    def identity(self) -> tuple[str, str, str]:
        return (domain.clean(self.name), self.category_text, domain.clean(self.specification))

    @property
    def corpus(self) -> tuple[str, str, str, str]:
        return (
            domain.clean(self.name), domain.clean(self.description),
            self.category_text, domain.clean(self.specification),
        )


#: Every field enrichment may fill, and what it is when no module fills it.
#: The keys are fixed: a record from an un-enriched source has the same shape
#: as any other, with nulls where the inference would have gone.
EMPTY: dict[str, Any] = {
    "family": None,
    "form": None,
    "firing": None,
    "surface": None,
    "effects": [],
    "colour": None,
    "application_methods": [],
    "coats": None,
    "claims": [],
    "package_size": None,
}


@dataclass(frozen=True)
class Module:
    """One named body of inference and the fields it owns.

    `run` is given the block the modules before it have filled, so a module
    that depends on another reads its answer rather than guessing again — two
    guesses at the same question is how a record ends up describing a liquid
    that its own `form` field calls a powder.
    """

    name: str
    fields: tuple[str, ...]
    run: Callable[[Context, dict[str, Any]], dict[str, Any]]
    #: Modules this one cannot work without, resolved before it runs.
    requires: tuple[str, ...] = field(default=())


def _classification(context: Context, derived: dict[str, Any]) -> dict[str, Any]:
    # Falls back to the full text only for the family: a shop that publishes
    # nothing but a code in the title has its department to go on, and losing
    # that costs the scope decision its only evidence.
    identity = context.identity
    return {
        "family": domain.family(*identity) or domain.family(*context.corpus),
        "form": domain.form(*identity),
    }


def _firing(context: Context, derived: dict[str, Any]) -> dict[str, Any]:
    return {"firing": domain.firing_range(*context.corpus)}


def _glaze(context: Context, derived: dict[str, Any]) -> dict[str, Any]:
    identity = context.identity
    return {
        "surface": domain.surface(*identity),
        "effects": domain.effects(*identity),
        "colour": domain.colour(context.name, context.colour_hint),
        "application_methods": domain.application_methods(*context.corpus),
        "coats": domain.coats(domain.clean(context.description), domain.clean(context.specification)),
    }


def _packaging(context: Context, derived: dict[str, Any]) -> dict[str, Any]:
    # Reading "1 pt" as a volume depends on knowing the product is a liquid,
    # which is what `classification` decided — hence the dependency rather than
    # a second, quietly different, guess at the family here. `form` is asked of
    # the full text for this one question: a description saying "pourable" is
    # evidence about the package even though it is not evidence about identity.
    liquid = (
        domain.form(*context.corpus) == "liquid"
        or derived.get("family") in {"glaze", "underglaze", "engobe"}
    )
    return {
        "package_size": domain.package_size(
            domain.clean(context.name), domain.clean(context.description), liquid_hint=liquid,
        ),
    }


def _claims(context: Context, derived: dict[str, Any]) -> dict[str, Any]:
    # Attribute blocks are turned into claims by the scraper that understands
    # them; reading them here would mistake a "not dinnerware safe" icon for a
    # positive claim.
    return {"claims": domain.claims(domain.clean(context.name), domain.clean(context.description))}


MODULES: dict[str, Module] = {
    module.name: module
    for module in (
        Module("classification", ("family", "form"), _classification),
        Module("firing", ("firing",), _firing),
        Module(
            "glaze",
            ("surface", "effects", "colour", "application_methods", "coats"),
            _glaze,
        ),
        Module("packaging", ("package_size",), _packaging, requires=("classification",)),
        Module("claims", ("claims",), _claims),
    )
}

#: Named sets, so a supplier of ceramic materials selects one thing rather than
#: five. The bundle is what every such source ran before enrichment could be
#: switched off at all, which is why it is spelled out here rather than being
#: "whatever modules happen to be registered".
BUNDLES: dict[str, tuple[str, ...]] = {
    "ceramic-materials": ("classification", "firing", "glaze", "packaging", "claims"),
}


def resolve(names: Iterable[str] | None) -> tuple[str, ...]:
    """Expand bundles into module names, in a fixed order, without duplicates.

    Raises on a name nobody registered. A typo in an enrichment list would
    otherwise silently drop the inference it was meant to select — the same
    class of failure `extra="forbid"` exists to prevent in the source config.
    """
    selected: list[str] = []
    for name in names or ():
        expanded = BUNDLES.get(name, (name,))
        for module in expanded:
            if module not in MODULES:
                known = ", ".join([*sorted(MODULES), *sorted(BUNDLES)])
                raise ValueError(f"unknown enrichment {name!r}; known: {known}")
            for dependency in (*MODULES[module].requires, module):
                if dependency not in selected:
                    selected.append(dependency)
    return tuple(name for name in MODULES if name in selected)


#: What a scope cannot function without, whatever the source selected.
#: `scope: materials` keeps a row only when it classifies as a ceramic material,
#: and that classification is this module's output — so the scope declaration is
#: itself a selection of it, and a materials source that named no enrichment
#: would otherwise crawl an empty catalogue rather than a narrow one.
SCOPE_REQUIRES: dict[str, tuple[str, ...]] = {"materials": ("classification",)}


def selected(scope: str | None, names: Iterable[str] | None) -> tuple[str, ...]:
    """The modules one source runs: what it asked for, plus what its scope needs."""
    return resolve((*SCOPE_REQUIRES.get(scope or "materials", ()), *(names or ())))


def apply(names: Sequence[str], context: Context) -> dict[str, Any]:
    """Run the selected modules and return the whole derived block."""
    derived = dict(EMPTY)
    for name in names:
        derived.update(MODULES[name].run(context, derived))
    return derived


def selected_for(traits: Mapping[str, Any]) -> tuple[str, ...]:
    """The modules one source's traits select, already expanded."""
    value = traits.get("enrichments")
    if isinstance(value, tuple):
        return value
    return resolve(value if isinstance(value, (list, tuple)) else None)
