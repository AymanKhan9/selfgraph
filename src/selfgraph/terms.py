from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from .ids import normalize_rel_type
from .schema import Schema, _entity_index, _lineage, canonical_entity_type, canonical_relation_type

__all__ = ["expand_tokens", "grounded_terms", "schema_forms", "stem"]

Tokenize = Callable[[str], set[str]]

_SINGULARS = {
    "calories": "calorie",
    "cookies": "cookie",
    "hoodies": "hoodie",
    "movies": "movie",
    "rookies": "rookie",
    "selfies": "selfie",
    "zombies": "zombie",
    "quizzes": "quiz",
    "buses": "bus",
    "statuses": "status",
    "status": "status",
    "analysis": "analysis",
    "news": "news",
    "series": "series",
    "species": "species",
}
_ES_AFTER = ("ss", "sh", "x", "zz", "tz")
_VOWELS = "aeiou"


def _takes_es(base: str) -> bool:
    """Report whether a singular forms its plural with "es", so the plural loses both letters.

    @param base (str): a plural without its final "es".
    @return (bool): True after ss, sh, x, zz, or tz, and after a consonant followed by ch.
    """
    if base.endswith(_ES_AFTER):
        return True
    return base.endswith("ch") and len(base) > 2 and base[-3] not in _VOWELS


def stem(token: str) -> str:
    """Strip a plural ending so "customers" and "customer", or "addresses" and "address", compare equal.

    The suffix rules run in order: -ies becomes -y, -es is dropped where _takes_es says the singular takes it, and
    any other final s is dropped unless the word ends in -ss. _SINGULARS holds the common words those rules get
    wrong: -ie nouns that would read as -y plurals, quizzes and buses (doubled or -us stems), status and analysis
    (singulars that end in s), and nouns that are the same in both numbers. A blanket -us / -is keep would leave
    menus unchanged and would turn excuses into excus.

    @param token (str): a casefolded token.
    @return (str): the token in its singular form.
    """
    if token in _SINGULARS:
        return _SINGULARS[token]
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 4 and _takes_es(token[:-2]):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        return token[:-1]
    return token


def _spaced(name: str) -> str:
    """Put spaces at camel-case and underscore boundaries, so InvoiceLine and WORKS_AT read as words.

    @param name (str): the type name.
    @return (str): the name as space-separated words.
    """
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")


def _forms(names: Iterable[str]) -> list[str]:
    """List the distinct surface forms of a term, each also split into words.

    @param names (Iterable[str]): the name, aliases, and synonyms.
    @return (list[str]): the forms, in order.
    """
    return list(dict.fromkeys(form for name in names for form in (name, _spaced(name))))


def schema_forms(schema: Schema | None) -> dict[str, dict[str, tuple[str, bool]]]:
    """Map every surface form of the schema's terms, by kind, to the term it names and whether it is certified.

    Entity types and relation types are named by name, alias, and synonym, and definitions by name and synonym; each
    form is also listed split into words, so InvoiceLine reads as "Invoice Line".

    @param schema (Schema | None): the schema, or None for no terms.
    @return (dict[str, dict[str, tuple[str, bool]]]): for "type", "definition", and "relation", each form's term and
        certified flag.
    """
    found: dict[str, dict[str, tuple[str, bool]]] = {"type": {}, "definition": {}, "relation": {}}
    if schema is None:
        return found

    def note(kind: str, term: str, names: Iterable[str], certified: bool) -> None:
        for form in _forms(names):
            found[kind].setdefault(form, (term, certified))

    for entity in schema.entities:
        note("type", entity.name, [entity.name, *entity.aliases, *entity.synonyms], entity.certified)
        for definition in entity.definitions:
            note("definition", definition.name, [definition.name, *definition.synonyms], definition.certified)
    for relation in schema.relations:
        note("relation", relation.type, [relation.type, *relation.aliases, *relation.synonyms], relation.certified)
    return found


def grounded_terms(
    schema: Schema | None, entity_types: Iterable[str], relation_types: Iterable[str]
) -> dict[str, set[str]]:
    """List the terms the stored graph holds instances of, by kind, so a schema term alone never grounds a question.

    A type is held when an entity of it or of a subtype is stored, a definition when its entity type is held, and a
    relation type when a relation of it is stored. The stored types themselves are held too.

    @param schema (Schema | None): the schema, or None for the stored types only.
    @param entity_types (Iterable[str]): the stored entities' types.
    @param relation_types (Iterable[str]): the stored relations' types.
    @return (dict[str, set[str]]): for "type", "definition", and "relation", the held term names.
    """
    types, rels = set(entity_types), set(relation_types)
    held: dict[str, set[str]] = {"type": set(types), "definition": set(), "relation": set(rels)}
    if schema is None:
        return held
    index = _entity_index(schema)
    for etype in types:
        entity = index.get(canonical_entity_type(etype, schema))
        if entity is not None:
            held["type"].update(t.name for t in _lineage(entity, index))
    held["definition"] = {d.name for e in schema.entities if e.name in held["type"] for d in e.definitions}
    stored = {normalize_rel_type(canonical_relation_type(t, schema)) for t in rels}
    held["relation"].update(r.type for r in schema.relations if normalize_rel_type(r.type) in stored)
    return held


def expand_tokens(tokens: set[str], schema: Schema | None, tokenize: Tokenize) -> set[str]:
    """Add the words of each entity type and its subtypes when a question names the type or a definition.

    A definition adds the words of the entity type it belongs to and that type's subtypes.

    @param tokens (set[str]): the question's content tokens.
    @param schema (Schema | None): the schema, or None for no expansion.
    @param tokenize (Tokenize): splits text into content tokens.
    @return (set[str]): the tokens with the matched type and subtype names added.
    """
    if schema is None:
        return tokens
    index = _entity_index(schema)
    owner = {d.name: e.name for e in schema.entities for d in e.definitions}
    stems = {stem(t) for t in tokens}
    out = set(tokens)
    forms = schema_forms(schema)
    for kind, named in (("type", lambda term: term), ("definition", owner.__getitem__)):
        for form, (term, _) in forms[kind].items():
            words = {stem(t) for t in tokenize(form)}
            if words and words <= stems:
                root = named(term)
                for entity in schema.entities:
                    if any(t.name == root for t in _lineage(entity, index)):
                        out |= tokenize(entity.name)
    return out
