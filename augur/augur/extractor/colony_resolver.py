"""Resolve Stellaris colonies independently from their physical carriers.

Stellaris 4.4 introduced Colony and Carrier as separate engine scopes. Colony
records hold population and economic state, while a carrier can be either a
planet or a ship (for example, an Arkship). Older saves store both concepts in
``planets.planet``. This module feature-detects the new section and presents one
stable representation to the extractor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CarrierType = Literal["planet", "ship", "unknown"]


@dataclass(frozen=True)
class ColonyRef:
    """A colony plus the planet or ship that carries it."""

    colony_id: str
    colony: dict
    carrier_type: CarrierType
    carrier_id: str | None
    carrier: dict
    schema: Literal["legacy_planet", "colony_carrier"]


@dataclass(frozen=True)
class ColonyResolution:
    """Resolved player colonies and any non-fatal compatibility warnings."""

    refs: tuple[ColonyRef, ...]
    schema: Literal["legacy_planet", "colony_carrier"]
    warnings: tuple[str, ...] = ()


def _as_id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int)):
        text = str(value).strip()
        return text or None
    return None


def _entity_map(section: object, *wrapper_keys: str) -> dict[str, dict]:
    """Normalize a direct entity map or a map nested under a wrapper key."""
    if not isinstance(section, dict):
        return {}

    candidate = section
    for key in wrapper_keys:
        nested = section.get(key)
        if isinstance(nested, dict):
            candidate = nested
            break

    return {
        str(entity_id): value for entity_id, value in candidate.items() if isinstance(value, dict)
    }


def _id_from_item(item: object, *, keys: tuple[str, ...]) -> str | None:
    direct = _as_id(item)
    if direct is not None:
        return direct
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = _as_id(item.get(key))
        if value is not None:
            return value
    return None


def _ids_from_collection(value: object, *, keys: tuple[str, ...]) -> list[str]:
    """Normalize Clausewitz list/dict variants into an ordered ID list."""
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        nested = value.get("items")
        if isinstance(nested, list):
            items = nested
        else:
            items = []
            for key, item in value.items():
                parsed = _id_from_item(item, keys=keys)
                items.append(parsed if parsed is not None else key)
    else:
        direct = _as_id(value)
        items = [direct] if direct is not None else []

    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        parsed = _id_from_item(item, keys=keys)
        if parsed is not None and parsed not in seen:
            seen.add(parsed)
            result.append(parsed)
    return result


def _carrier_hint(
    value: object, default_type: CarrierType = "unknown"
) -> tuple[CarrierType, str | None]:
    """Read the common serialized forms of a carrier reference."""
    direct = _as_id(value)
    if direct is not None:
        return default_type, direct
    if not isinstance(value, dict):
        return default_type, None

    for key, carrier_type in (("planet", "planet"), ("ship", "ship")):
        carrier_id = _as_id(value.get(key))
        if carrier_id is not None:
            return carrier_type, carrier_id

    raw_type = value.get("type") or value.get("kind") or value.get("scope")
    carrier_type: CarrierType = default_type
    if isinstance(raw_type, str):
        lowered = raw_type.lower()
        if "planet" in lowered:
            carrier_type = "planet"
        elif "ship" in lowered or "arkship" in lowered:
            carrier_type = "ship"

    for key in ("reference", "id", "carrier_id", "key", "value"):
        carrier_id = _as_id(value.get(key))
        if carrier_id is not None:
            return carrier_type, carrier_id
    return carrier_type, None


def _ship_colony_hint(ship_colonies: object, colony_id: str) -> tuple[CarrierType, str | None]:
    """Resolve either colony->ship or ship->colony map layouts."""
    if not isinstance(ship_colonies, dict):
        return "unknown", None

    direct = ship_colonies.get(colony_id)
    carrier_type, carrier_id = _carrier_hint(direct, "ship")
    if carrier_id is not None:
        return carrier_type, carrier_id

    for ship_id, value in ship_colonies.items():
        linked_colony = _id_from_item(value, keys=("colony", "colony_id", "id"))
        if linked_colony == colony_id:
            return "ship", str(ship_id)
    return "unknown", None


def _linked_colony_id(carrier: dict) -> str | None:
    for key in ("colony", "colony_id", "controlled_colony"):
        linked = _as_id(carrier.get(key))
        if linked is not None:
            return linked
    return None


def _resolve_carrier(
    colony_id: str,
    colony: dict,
    *,
    planets: dict[str, dict],
    ships: dict[str, dict],
    ship_colonies: object,
) -> tuple[CarrierType, str | None, dict]:
    carrier_type: CarrierType = "unknown"
    carrier_id: str | None = None

    for key, default_type in (
        ("carrier", "unknown"),
        ("carrier_planet", "planet"),
        ("planet", "planet"),
        ("planet_id", "planet"),
        ("carrier_ship", "ship"),
        ("ship", "ship"),
        ("ship_id", "ship"),
    ):
        if key not in colony:
            continue
        carrier_type, carrier_id = _carrier_hint(colony.get(key), default_type)
        if carrier_id is not None:
            break

    if carrier_id is None:
        carrier_type, carrier_id = _ship_colony_hint(ship_colonies, colony_id)

    if carrier_id is None:
        planet_matches = [
            key for key, value in planets.items() if _linked_colony_id(value) == colony_id
        ]
        ship_matches = [
            key for key, value in ships.items() if _linked_colony_id(value) == colony_id
        ]
        if len(planet_matches) == 1 and not ship_matches:
            carrier_type, carrier_id = "planet", planet_matches[0]
        elif len(ship_matches) == 1 and not planet_matches:
            carrier_type, carrier_id = "ship", ship_matches[0]

    # Some transitional shapes retain identical colony/carrier IDs.
    if carrier_id is None:
        in_planets = colony_id in planets
        in_ships = colony_id in ships
        if in_planets != in_ships:
            carrier_type = "planet" if in_planets else "ship"
            carrier_id = colony_id

    if carrier_id is None:
        return "unknown", None, {}

    in_planets = carrier_id in planets
    in_ships = carrier_id in ships
    if carrier_type == "unknown" and in_planets != in_ships:
        carrier_type = "planet" if in_planets else "ship"

    if carrier_type == "planet":
        return carrier_type, carrier_id, planets.get(carrier_id, {})
    if carrier_type == "ship":
        return carrier_type, carrier_id, ships.get(carrier_id, {})
    return carrier_type, carrier_id, {}


def candidate_carrier_ids(colonies_section: object, ship_colonies: object = None) -> list[str]:
    """Return carrier IDs worth fetching from the large ``ships`` section."""
    colonies = _entity_map(colonies_section, "colony", "colonies")
    candidates: list[str] = []
    for colony_id, colony in colonies.items():
        _carrier_type, carrier_id, _carrier = _resolve_carrier(
            colony_id,
            colony,
            planets={},
            ships={},
            ship_colonies=ship_colonies,
        )
        if carrier_id is not None:
            candidates.append(carrier_id)

    if isinstance(ship_colonies, dict):
        for key, value in ship_colonies.items():
            candidates.append(str(key))
            _carrier_type, carrier_id = _carrier_hint(value, "ship")
            if carrier_id is not None:
                candidates.append(carrier_id)

    return list(dict.fromkeys(candidates))


def resolve_player_colonies(
    *,
    country: dict,
    player_id: int | str,
    colonies_section: object,
    planets_section: object,
    ships_section: object = None,
    ship_colonies: object = None,
) -> ColonyResolution:
    """Resolve the selected country's colonies for legacy and 4.4+ saves."""
    player_id_str = str(player_id)
    planets = _entity_map(planets_section, "planet", "planets")
    ships = _entity_map(ships_section, "ship", "ships")
    colonies = _entity_map(colonies_section, "colony", "colonies")

    if not colonies:
        owned_ids = _ids_from_collection(
            country.get("owned_planets", []), keys=("planet", "planet_id", "id")
        )
        if not owned_ids:
            owned_ids = [
                planet_id
                for planet_id, planet in planets.items()
                if str(planet.get("owner")) == player_id_str
            ]

        refs = tuple(
            ColonyRef(
                colony_id=planet_id,
                colony=planets[planet_id],
                carrier_type="planet",
                carrier_id=planet_id,
                carrier=planets[planet_id],
                schema="legacy_planet",
            )
            for planet_id in owned_ids
            if planet_id in planets
        )
        return ColonyResolution(refs=refs, schema="legacy_planet")

    warnings: list[str] = []
    all_refs = {
        colony_id: ColonyRef(
            colony_id=colony_id,
            colony=colony,
            carrier_type=carrier_type,
            carrier_id=carrier_id,
            carrier=carrier,
            schema="colony_carrier",
        )
        for colony_id, colony in colonies.items()
        for carrier_type, carrier_id, carrier in [
            _resolve_carrier(
                colony_id,
                colony,
                planets=planets,
                ships=ships,
                ship_colonies=ship_colonies,
            )
        ]
    }

    candidate_groups = (
        _ids_from_collection(
            country.get("controlled_colonies", []), keys=("colony", "colony_id", "id")
        ),
        _ids_from_collection(country.get("colonies", []), keys=("colony", "colony_id", "id")),
        _ids_from_collection(country.get("ship_colonies", []), keys=("colony", "colony_id", "id")),
    )
    selected_ids: list[str] = []
    for candidate_ids in candidate_groups:
        if not candidate_ids:
            continue
        present = [colony_id for colony_id in candidate_ids if colony_id in all_refs]
        for colony_id in candidate_ids:
            if colony_id not in all_refs:
                warnings.append(f"Player colony {colony_id} is absent from the colonies section")
        if present:
            selected_ids = present
            break

    owned_planets = _ids_from_collection(
        country.get("owned_planets", []), keys=("planet", "planet_id", "id")
    )
    if not selected_ids and owned_planets:
        direct = [entity_id for entity_id in owned_planets if entity_id in all_refs]
        selected_ids = direct or [
            colony_id
            for colony_id, ref in all_refs.items()
            if ref.carrier_type == "planet" and ref.carrier_id in owned_planets
        ]

    if not selected_ids:
        selected_ids = [
            colony_id
            for colony_id, colony in colonies.items()
            if str(colony.get("owner")) == player_id_str
        ]

    refs: list[ColonyRef] = []
    for colony_id in selected_ids:
        ref = all_refs.get(colony_id)
        if ref is None:
            continue
        if ref.carrier_id is None:
            warnings.append(f"Player colony {colony_id} has no resolvable carrier")
        elif not ref.carrier:
            warnings.append(
                f"Player colony {colony_id} references missing {ref.carrier_type} carrier {ref.carrier_id}"
            )
        refs.append(ref)

    return ColonyResolution(
        refs=tuple(refs),
        schema="colony_carrier",
        warnings=tuple(warnings),
    )
