"""What Cairn asks Graphiti to extract from a memory.

Graphiti records a fact as a relationship between two entities, so a memory
from which it extracts fewer than two entities leaves nothing in the graph.
Its stock entity prompt is written for chat transcripts ("when in doubt, do
NOT extract"; "could this have its own Wikipedia article?") and, called without
entity types or instructions, it extracted fewer than two entities from most
one-sentence memories: traced on 22 November 2026, 9 to 10 of 28 synthetic
sentences left any fact, with every loss at entity extraction and none at edge
extraction or resolution.

Both values below reach the library's prompts, so both are key material for
the extraction cache (``graphiti_extraction_cache.extraction_cache_key``):
changing either re-extracts on the next rebuild.

The examples are deliberately not drawn from any fixture corpus; a test holds
that, so a measurement on those corpora stays a measurement.
"""

from pydantic import BaseModel


class Person(BaseModel):
    """A named individual."""


class Organization(BaseModel):
    """A team, company, department or other group of people, named or
    specifically qualified (for example 'payments team')."""


class Project(BaseModel):
    """A project, initiative, rollout, migration, review, release or other
    piece of work, named or specifically qualified (for example 'checkout
    rewrite')."""


class System(BaseModel):
    """A service, application, pipeline, tool, command, machine, component or
    part, named or specifically qualified (for example 'payroll service')."""


class Artifact(BaseModel):
    """A document, file, path, setting, checklist, policy, rota or other work
    product, named or specifically qualified (for example 'incident
    runbook')."""


class Place(BaseModel):
    """A location, site, office, building, route or part of one (for example
    'Geneva office')."""


class Topic(BaseModel):
    """A preference, rule, practice, property or value stated about something,
    in the words of the text (for example 'tabs over spaces', 'ten-day
    expiry')."""


# The types carry no fields on purpose: a type with fields makes the library
# run one attribute-extraction call per entity, and Cairn reads no attributes.
ENTITY_TYPES: dict[str, type[BaseModel]] = {
    model.__name__: model
    for model in (Person, Organization, Project, System, Artifact, Place, Topic)
}

EXTRACTION_INSTRUCTIONS = (
    "The text is one stored memory. Extract every person, team, organisation, project, "
    "system, service, tool, file, document, place and other thing the memory is about, "
    "when it is named or specifically qualified in the text (for example 'payroll "
    "service', 'incident runbook', 'Geneva office'). A qualified noun phrase is specific "
    "enough; it need not be a proper noun. When the memory states a preference, rule, "
    "property or value of a single subject, also extract what is stated as a short "
    "entity in the words of the text (for example from 'Dana prefers tabs over spaces' "
    "extract 'Dana' and 'tabs over spaces'; from 'Exports expire after ten days' extract "
    "'exports' and 'ten-day expiry'), so that the statement has two entities to relate."
)
