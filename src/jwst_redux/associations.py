"""Official association generation from authoritative local JWST FITS headers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import PipelineExecutionError


@dataclass(frozen=True)
class LocalAssociation:
    path: Path
    content_sha256: str
    member_paths: tuple[Path, ...]
    product_name: str
    reused: bool
    official_rule: str
    source: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "content_sha256": self.content_sha256,
            "product_name": self.product_name,
            "official_rule": self.official_rule,
            "source": self.source,
            "members": json.loads(self.path.read_text())["products"][0]["members"],
            "member_files": [
                {"path": str(p), "size_bytes": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
                for p in self.member_paths
            ],
        }


def generate_level3_associations(
    paths: tuple[Path, ...], association_dir: Path, rule_name: str
) -> tuple[LocalAssociation, ...]:
    """Generate official Level-3 associations from calibrated FITS headers.

    mkpool supplies observation candidates and target identity. Virtual Level-1b
    names enable official filename transformations without modifying any FITS.
    The mode's registered rule decides which exposures belong together.
    """
    from astropy.io import fits
    from jwst.associations import AssociationRegistry, generate
    from jwst.associations.lib.utilities import constrain_on_candidates
    from jwst.associations.mkpool import mkpool

    ordered = tuple(sorted(p.resolve() for p in paths))
    if not ordered or len(ordered) != len(set(ordered)):
        raise PipelineExecutionError("Association inputs must be non-empty and unique.")
    by_name = {p.name: p for p in ordered}
    if len(by_name) != len(ordered):
        raise PipelineExecutionError("Association input filenames must be unique.")
    # Fail before association generation if optical/target identity is incomplete.
    required = (
        "PROGRAM",
        "OBSERVTN",
        "VISIT",
        "TARGNAME",
        "INSTRUME",
        "EXP_TYPE",
        "DETECTOR",
        "GRATING",
        "FILTER",
        "FXD_SLIT",
        "SUBARRAY",
        "TSOVISIT",
    )
    for path in ordered:
        header = fits.getheader(path)
        fields = required
        missing = [key for key in fields if key not in header or not str(header[key]).strip()]
        if missing:
            raise PipelineExecutionError(f"Association metadata missing in {path.name}: {missing}")
    pool = mkpool([str(p) for p in ordered])
    for column in pool.colnames:
        pool[column] = [str(value).lower() for value in pool[column]]
    suffix = "_cal.fits"
    if any(not p.name.endswith(suffix) for p in ordered):
        raise PipelineExecutionError(f"Level 3 association requires {suffix} inputs.")
    pool["filename"] = [p.name.removesuffix(suffix) + "_uncal.fits" for p in ordered]
    registry = AssociationRegistry(global_constraints=constrain_on_candidates(None))
    if rule_name not in registry:
        raise PipelineExecutionError(
            f"Installed jwst does not provide association rule {rule_name}."
        )
    for name in list(registry):
        if name != rule_name:
            del registry[name]
    associations = generate(pool, registry)
    if not associations:
        raise PipelineExecutionError(f"Official {rule_name} generated no valid associations.")
    results = []
    science_names = []
    for association in associations:
        for product in association["products"]:
            for member in product["members"]:
                name = Path(member["expname"]).name
                if name not in by_name:
                    raise PipelineExecutionError(
                        f"Official association member is unavailable: {name}"
                    )
                if member["exptype"] == "science":
                    science_names.append(name)
                member["expname"] = os.path.relpath(by_name[name], association_dir)
            product["members"].sort(key=lambda m: (m["exptype"] != "science", m["expname"]))
        # Constraint repr may include function memory addresses. This is
        # explanatory metadata, not a pipeline parameter or member decision.
        association["constraints"] = re.sub(
            r" at 0x[0-9a-fA-F]+", "", association.get("constraints", "")
        )
        association.validate(association)
        _, serialized = association.dump(format="json")
        canonical = json.dumps(json.loads(serialized), indent=2, sort_keys=True) + "\n"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        product_name = association["products"][0]["name"]
        filename = f"{rule_name.lower()}-{digest[:16]}_asn.json"
        path = association_dir / filename
        association_dir.mkdir(parents=True, exist_ok=True)
        reused = path.is_file() and path.read_text() == canonical
        if not reused:
            path.write_text(canonical)
        member_paths = tuple(
            (association_dir / m["expname"]).resolve()
            for p in association["products"]
            for m in p["members"]
        )
        results.append(
            LocalAssociation(path, digest, member_paths, product_name, reused, rule_name)
        )
    if sorted(science_names) != sorted(by_name):
        raise PipelineExecutionError(
            "Official associations do not cover each selected science input exactly once."
        )
    return tuple(sorted(results, key=lambda a: a.product_name))


def resolve_level2_associations(
    archive_products: tuple[Any, ...],
    rate_paths: tuple[Path, ...],
    association_dir: Path,
    *,
    downloader: Any = None,
) -> tuple[LocalAssociation, ...]:
    """Resolve archived spec2 assignments without reconstructing nod science logic.

    EXPSPCIN is archive-pool metadata absent from ordinary FITS headers, so
    mkpool alone cannot safely run Asn_Lv2NRSFSS on arbitrary public exposures.
    Never silently substitute a science-only association when the archive ASN
    or one of its members is unavailable.
    """
    from jwst.associations import load_asn
    from jwst.associations.lib.rules_level2_base import DMSLevel2bBase

    from .mast.download import ensure_downloaded

    available = {p.name: p.resolve() for p in rate_paths}
    if not available or len(available) != len(rate_paths):
        raise PipelineExecutionError("Level-2 rate inputs must be non-empty and unique.")
    cache = association_dir / "archive"
    cache.mkdir(parents=True, exist_ok=True)
    products = {p.filename: p for p in archive_products}
    if not products:
        raise PipelineExecutionError(
            "No archived Level-2 associations were discovered; cannot safely assign nod backgrounds."
        )
    candidates: dict[str, list[LocalAssociation]] = {}
    for product in sorted(products.values(), key=lambda p: p.filename):
        download = ensure_downloaded(product, cache, overwrite=True, downloader=downloader)
        text = download.path.read_text()
        try:
            association = load_asn(text, validate=True)
        except Exception as error:
            raise PipelineExecutionError(
                f"Invalid official archive association: {product.filename}"
            ) from error
        if association.get("asn_type") != "spec2":
            continue
        if not association.get("asn_rule", "").endswith("Asn_Lv2NRSFSS"):
            continue
        if len(association["products"]) != 1:
            raise PipelineExecutionError("Archived spec2 association must contain one product.")
        output = association["products"][0]
        science = [m for m in output["members"] if m["exptype"] == "science"]
        if len(science) != 1:
            raise PipelineExecutionError(
                "Archived spec2 association must contain one science exposure."
            )
        science_name = Path(science[0]["expname"]).name
        if science_name not in available:
            continue
        # Observation candidates are the safest reproducible default; discovered
        # associations can aggregate an accidental subset of a nod sequence.
        if not str(association.get("asn_id", "")).startswith("o"):
            continue
        missing = [
            m["expname"] for m in output["members"] if Path(m["expname"]).name not in available
        ]
        if missing:
            raise PipelineExecutionError(
                "Archived nod association requires members outside the complete selected group: "
                + ", ".join(missing)
            )
        if Path(output["name"]).name != science_name.removesuffix("_rate.fits"):
            raise PipelineExecutionError("Archived spec2 product does not match its science input.")
        member_paths = tuple(available[Path(m["expname"]).name] for m in output["members"])
        for member, path in zip(output["members"], member_paths, strict=True):
            member["expname"] = os.path.relpath(path, association_dir)
        DMSLevel2bBase.validate(association)
        canonical = json.dumps(association, sort_keys=True, indent=2) + "\n"
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        local = association_dir / f"{output['name']}-{digest[:16]}_asn.json"
        reused = local.is_file() and local.read_text() == canonical
        if not reused:
            local.write_text(canonical)
        result = LocalAssociation(
            local,
            digest,
            member_paths,
            output["name"],
            reused,
            association["asn_rule"],
            {
                "uri": product.uri,
                "filename": product.filename,
                "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
        )
        candidates.setdefault(science_name, []).append(result)
    resolved = []
    for name in sorted(available):
        matches = candidates.get(name, [])
        memberships = {
            tuple(
                sorted(
                    (Path(m["expname"]).name, m["exptype"])
                    for m in json.loads(a.path.read_text())["products"][0]["members"]
                )
            )
            for a in matches
        }
        if not matches:
            raise PipelineExecutionError(f"No official archived nod association for {name}.")
        if len(memberships) != 1:
            raise PipelineExecutionError(f"Ambiguous archived nod associations for {name}.")
        # If the archive contains identical revisions, the stable filename order
        # above determines which provenance source is used.
        resolved.append(matches[0])
    return tuple(resolved)
