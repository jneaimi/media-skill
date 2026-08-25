"""Compile social-ad specs into the established storyboard pipeline.

Ads are deliberately lowered into ordinary stories so the already-proven board, bridge,
and assembly chain remains unchanged.  Platform safe zones reserve the pixels covered by
social UI, keeping later-burned copy readable.  This module is intentionally stdlib-only:
it neither imports Pillow or ffmpeg nor makes network requests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _weights(values: tuple[float, ...]) -> tuple[float, ...]:
    """Normalise authored proportions once, rather than relying on float hand-tuning."""
    total = sum(values)
    return tuple(value / total for value in values)


def _format(roles, weights, faceless, needs_actor, needs_product, summary):
    return {
        "roles": tuple(roles), "weights": _weights(tuple(weights)), "faceless": faceless,
        "needs_actor": needs_actor, "needs_product": needs_product, "summary": summary,
        "guidance": {role: f"Make the {role.replace('_', ' ')} beat visually specific and decisive."
                     for role in roles},
    }


AD_FORMATS: dict[str, dict] = {
    "problem-solution": _format(("hook", "problem", "solution", "proof", "cta"),
        (15, 20, 30, 20, 15), False, True, True,
        "Present a relatable problem, agitate it, introduce the product as the fix."),
    "testimonial": _format(("hook", "context", "result", "cta"), (20, 25, 35, 20),
        False, True, True, "Let a credible person describe a concrete product result."),
    "unboxing": _format(("hook", "reveal", "detail", "first_use", "cta"),
        (15, 25, 20, 25, 15), False, True, True, "Reveal and use the product with tactile detail."),
    "before-after": _format(("before", "transition", "after", "cta"), (25, 15, 40, 20),
        False, True, True, "Make the change legible from a clear before to a convincing after."),
    "tutorial": _format(("hook", "step_one", "step_two", "step_three", "cta"),
        (15, 22, 24, 24, 15), False, True, True, "Teach a simple repeatable use of the product."),
    "hero-product": _format(("establish", "detail", "logo"), (35, 45, 20), True, False, True,
        "Use product-first imagery to make materials and form memorable."),
    "asmr": _format(("texture", "action", "settle"), (35, 40, 25), True, False, True,
        "Use satisfying close visual actions and a calm final composition."),
    "kinetic-text": _format(("hook", "point_one", "point_two", "payoff"), (25, 25, 25, 25),
        True, False, False, "Create a clear visual rhythm around a concise idea."),
    "explainer": _format(("hook", "context", "mechanism", "cta"), (20, 25, 35, 20),
        True, False, False, "Explain the mechanism with a simple visual progression."),
    "hands-demo": _format(("hook", "setup", "demo", "cta"), (20, 25, 35, 20),
        True, False, True, "Show hands performing the product's useful action."),
    "custom": _format((), (), False, False, False, "Use author-defined beats."),
}
for _name, _entry in AD_FORMATS.items():
    assert not _entry["weights"] or abs(sum(_entry["weights"]) - 1.0) < 1e-9, _name

PLATFORMS: dict[str, dict] = {
    "tiktok": {"aspect": "9:16", "width": 1080, "height": 1920,
               "insets": {"top": .10, "right": .10, "bottom": .20, "left": 0.0},
               "max_seconds": 600, "recommended": (21, 34)},
    "reels": {"aspect": "9:16", "width": 1080, "height": 1920,
              "insets": {"top": .10, "right": .12, "bottom": .20, "left": 0.0},
              "max_seconds": 90, "recommended": (15, 30)},
    "shorts": {"aspect": "9:16", "width": 1080, "height": 1920,
               "insets": {"top": .08, "right": .12, "bottom": .18, "left": 0.0},
               "max_seconds": 180, "recommended": (15, 60)},
    "feed": {"aspect": "1:1", "width": 1080, "height": 1080,
             "insets": {"top": .05, "right": .05, "bottom": .10, "left": .05},
             "max_seconds": 240, "recommended": (15, 30)},
    "youtube": {"aspect": "16:9", "width": 1920, "height": 1080,
                "insets": {"top": .05, "right": .05, "bottom": .10, "left": .05},
                "max_seconds": 0, "recommended": (15, 30)},
}
CAPTION_ROLES = ("hook", "caption", "cta", "disclosure", "lower_third")
DISCLOSURE_DEFAULT = "AI-generated"


class AdSpecError(Exception):
    """Raised when an ad spec is malformed; the message names the offending JSON path."""


def load_ad_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AdSpecError(f"{path}: invalid JSON: {exc}")
    if not isinstance(spec, dict):
        raise AdSpecError(f"{path}: top level must be a JSON object")
    validate_ad_spec(spec)
    return spec


def format_for(name: str) -> dict:
    if name not in AD_FORMATS:
        raise AdSpecError(f"spec.format {name!r} is unknown\n  Use one of: {', '.join(sorted(AD_FORMATS))}")
    return AD_FORMATS[name]


def platform_for(name: str) -> dict:
    if name not in PLATFORMS:
        raise AdSpecError(f"spec.platform {name!r} is unknown\n  Use one of: {', '.join(sorted(PLATFORMS))}")
    return PLATFORMS[name]


def _camera_warnings(beats: list[dict]) -> list[str]:
    sys.path.insert(0, str(Path(__file__).parent))
    import storyboard
    return [f"spec.beats[{i}].camera {beat['camera']!r} is outside the known vocabulary"
            for i, beat in enumerate(beats) if beat.get("camera") and beat["camera"] not in storyboard.CAMERA_VOCABULARY]


def check_claims(spec: dict) -> list[str]:
    product = spec.get("product") or {}
    claims = product.get("banned_claims") if isinstance(product, dict) else None
    if not isinstance(claims, list):
        return []
    texts = []
    for beat in spec.get("beats") or []:
        if isinstance(beat, dict):
            texts.append((beat.get("id", "?"), " ".join(str(beat.get(k, "")) for k in ("say", "caption"))))
    cta = spec.get("cta") or {}
    if isinstance(cta, dict):
        texts.append(("cta", str(cta.get("text", ""))))
    warnings = []
    for phrase in claims:
        if not isinstance(phrase, str):
            continue
        for beat_id, text in texts:
            if phrase.lower() in text.lower():
                warnings.append(f"spec.beats[{beat_id}]: banned claim {phrase!r} appears in copy")
    return warnings


def validate_ad_spec(spec: dict) -> list[str]:
    if not isinstance(spec, dict):
        raise AdSpecError("spec: top level must be an object")
    if spec.get("version") != 1:
        raise AdSpecError("spec.version must be 1")
    if "kind" in spec and spec["kind"] != "ad":
        raise AdSpecError("spec.kind must be 'ad'")
    known = {"version", "kind", "title", "format", "platform", "duration", "model", "provider", "resolution", "style", "product", "actor", "cast", "beats", "captions", "cta", "disclosure", "board", "allow_text"}
    unknown = sorted(set(spec) - known)
    if unknown:
        raise AdSpecError(f"unknown top-level key(s): {', '.join(unknown)}")
    fmt_name, platform_name = spec.get("format"), spec.get("platform")
    if not isinstance(fmt_name, str):
        raise AdSpecError("spec.format is required and must be a string")
    fmt = format_for(fmt_name)
    if not isinstance(platform_name, str):
        raise AdSpecError("spec.platform is required and must be a string")
    platform = platform_for(platform_name)
    duration = spec.get("duration")
    if type(duration) is not int or duration < 4:
        raise AdSpecError("spec.duration must be an integer of at least 4 seconds")
    beats = spec.get("beats")
    if not isinstance(beats, list) or not beats:
        raise AdSpecError("spec.beats must be a non-empty array")
    ids, seen = [], set()
    beat_keys = {"id", "panel", "action", "camera", "say", "lang", "caption", "sound", "duration", "ending", "allow_text"}
    for index, beat in enumerate(beats):
        where = f"spec.beats[{index}]"
        if not isinstance(beat, dict): raise AdSpecError(f"{where} must be an object")
        extras = sorted(set(beat) - beat_keys)
        if extras: raise AdSpecError(f"{where}: unknown key(s): {', '.join(extras)}")
        ident = beat.get("id")
        if not isinstance(ident, str) or not ident: raise AdSpecError(f"{where}.id is required")
        if ident in seen: raise AdSpecError(f"{where}.id {ident!r} is duplicated")
        seen.add(ident); ids.append(ident)
        if not isinstance(beat.get("panel"), str) or not beat["panel"]: raise AdSpecError(f"{where}.panel is required")
        if index < len(beats) - 1 and not beat.get("action"): raise AdSpecError(f"{where}.action is required")
        if "duration" in beat and (type(beat["duration"]) is not int or beat["duration"] <= 0): raise AdSpecError(f"{where}.duration must be a positive integer")
    roles = fmt["roles"]
    if fmt_name != "custom" and (len(beats) != len(roles) or tuple(ids) != roles):
        raise AdSpecError(f"spec.beats: format {fmt_name!r} expects ids {', '.join(roles)} — got {', '.join(ids)}")
    allocation_format = fmt if roles else {
        "roles": tuple(ids), "weights": tuple(1 / len(ids) for _ in ids),
    }
    # Validate the floor before any image or video work starts; a five-beat 12-second
    # request cannot become valid later in the pipeline.
    allocate_beats(allocation_format, duration, overrides=[beat.get("duration") for beat in beats])
    product = spec.get("product")
    if product is not None and not isinstance(product, dict): raise AdSpecError("spec.product must be an object")
    refs = product.get("refs") if isinstance(product, dict) else None
    if refs is not None and (not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs)): raise AdSpecError("spec.product.refs must be a list of strings")
    if fmt["needs_product"] and not refs: raise AdSpecError("spec.product.refs needs at least one product reference")
    if "cast" in spec and not isinstance(spec["cast"], dict): raise AdSpecError("spec.cast must be an object")
    if "board" in spec and not isinstance(spec["board"], dict): raise AdSpecError("spec.board must be an object")
    actor = spec.get("actor")
    if actor is not None and not isinstance(actor, dict): raise AdSpecError("spec.actor must be an object")
    if fmt["needs_actor"] and not (isinstance(actor, dict) and actor.get("ref") or spec.get("cast")):
        raise AdSpecError("spec.actor.ref or spec.cast is required for this format")
    cta = spec.get("cta")
    if cta is not None and (not isinstance(cta, dict) or not cta.get("text")):
        raise AdSpecError("spec.cta.text is required when spec.cta is present")
    captions = spec.get("captions")
    if captions is not None:
        if not isinstance(captions, dict): raise AdSpecError("spec.captions must be an object")
        extras = sorted(set(captions) - {"style", "font_size", "burn", "position", "hook"})
        if extras: raise AdSpecError(f"spec.captions: unknown key(s): {', '.join(extras)}")
    warnings = []
    low, high = platform["recommended"]
    if not low <= duration <= high: warnings.append(f"spec.duration {duration}s is outside {platform_name}'s recommended {low}-{high}s range")
    if platform["max_seconds"] and duration > platform["max_seconds"]: warnings.append(f"spec.duration {duration}s exceeds {platform_name}'s {platform['max_seconds']}s maximum")
    warnings.extend(_camera_warnings(beats)); warnings.extend(check_claims(spec))
    if fmt["faceless"] and actor: warnings.append(f"spec.actor is supplied but format {fmt_name!r} is faceless")
    if not spec.get("disclosure"): warnings.append('spec.disclosure is absent or false; EU AI Act Article 50 has required marking of synthetic media since 2026-08-02, and "disclosure": true adds it')
    return warnings


def safe_zone(platform: str, width: int | None = None, height: int | None = None) -> dict:
    entry = platform_for(platform)
    width, height = entry["width"] if width is None else width, entry["height"] if height is None else height
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0: raise AdSpecError("safe_zone dimensions must be positive integers")
    inset = entry["insets"]; top, bottom = round(inset["top"] * height), round(inset["bottom"] * height)
    left, right = round(inset["left"] * width), round(inset["right"] * width)
    box = (left, top, width - right, height - bottom)
    if box[2] <= box[0] or box[3] <= box[1]: raise AdSpecError("safe_zone.box has zero or negative area")
    return {"width": width, "height": height, "top": top, "right": right, "bottom": bottom, "left": left, "box": box}


def allocate_beats(fmt: dict, total: int, *, minimum: int = 4, maximum: int = 15, overrides: list[int | None] | None = None) -> list[int]:
    roles, weights = fmt["roles"], fmt["weights"]; count = len(roles)
    if count == 0: raise AdSpecError("format.roles must contain at least one beat")
    if overrides is None: overrides = [None] * count
    if len(overrides) != count: raise AdSpecError("overrides must have one entry per beat")
    if total < count * minimum: raise AdSpecError(f"duration {total}s cannot cover {count} beats at {minimum}s minimum — needs at least {count * minimum}s")
    result = [0] * count
    for index, value in enumerate(overrides):
        if value is not None:
            if type(value) is not int or value < minimum or value > maximum: raise AdSpecError(f"overrides[{index}] must be between {minimum} and {maximum}")
            result[index] = value
    free = [i for i, value in enumerate(overrides) if value is None]
    remaining = total - sum(result)
    if remaining < len(free) * minimum: raise AdSpecError(f"duration {total}s cannot cover unpinned beats at {minimum}s minimum")
    if not free: return result
    # Reserve the floor, then apportion the remaining seconds by Hare's largest remainders.
    pool = remaining - len(free) * minimum
    for index in free:
        result[index] = minimum
    while pool:
        active = [index for index in free if result[index] < maximum]
        if not active:
            break
        total_weight = sum(weights[index] for index in active)
        shares = {index: pool * weights[index] / total_weight for index in active}
        floors = {index: min(maximum - result[index], int(shares[index])) for index in active}
        used = sum(floors.values())
        for index in active:
            result[index] += floors[index]
        pool -= used
        if not pool:
            break
        candidates = [index for index in active if result[index] < maximum]
        if not candidates:
            break
        # Floors may consume nothing for a small pool; the fractional ranking still moves
        # one second and gives exact Hare tie-breaking by original index.
        for index in sorted(candidates, key=lambda i: (-(shares[i] - int(shares[i])), i)):
            if not pool:
                break
            result[index] += 1
            pool -= 1
    return result


def beat_timing(durations: list[int], ids: list[str], roles: tuple[str, ...] | list[str]) -> list[dict]:
    cursor = 0.0; resolved = []
    for index, (duration, ident) in enumerate(zip(durations, ids)):
        end = cursor + float(duration)
        resolved.append({"id": ident, "index": index, "duration": duration, "start": cursor, "end": end, "role": roles[index] if index < len(roles) else ident})
        cursor = end
    return resolved


def compile_to_story(spec: dict) -> dict:
    warnings = validate_ad_spec(spec)[:]
    fmt, platform = format_for(spec["format"]), platform_for(spec["platform"])
    allocation_format = fmt if fmt["roles"] else {
        "roles": tuple(beat["id"] for beat in spec["beats"]),
        "weights": tuple(1 / len(spec["beats"]) for _ in spec["beats"]),
    }
    durations = allocate_beats(allocation_format, spec["duration"], overrides=[beat.get("duration") for beat in spec["beats"]])
    resolved = beat_timing(durations, [beat["id"] for beat in spec["beats"]], fmt["roles"])
    cast = dict(spec.get("cast") or {})
    for number, ref in enumerate((spec.get("product") or {}).get("refs") or [], 1): cast[f"product_{number}"] = ref
    actor = spec.get("actor") or {}
    actor_name = actor.get("name", "Actor") if isinstance(actor, dict) else "Actor"
    if isinstance(actor, dict) and actor.get("ref"): cast[actor_name] = actor["ref"]
    story = {"version": 1, "aspect": platform["aspect"], "chain": "bridge", "board": dict(spec.get("board") or {}), "shots": [], "allow_text": False}
    for key in ("title", "model", "provider", "resolution", "style"):
        if key in spec: story[key] = spec[key]
    if cast: story["cast"] = cast
    for beat, timing in zip(spec["beats"], resolved):
        shot = {key: beat[key] for key in ("id", "panel", "action", "camera", "duration", "ending") if key in beat}
        shot["duration"] = timing["duration"]
        sound = dict(beat.get("sound") or {})
        if beat.get("say"):
            if sound.get("dialogue"): warnings.append(f"spec.beats[{beat['id']}].say ignored because sound.dialogue is explicit")
            else: sound["dialogue"] = [{"speaker": actor_name if isinstance(actor, dict) and actor.get("ref") else "Narrator", "lang": beat.get("lang", "English"), "line": beat["say"]}]
        if sound: shot["sound"] = sound
        # The last authored beat still generates a bridge clip into the synthesised end
        # frame. Storyboard therefore requires an action even though ad validation lets a
        # closing beat omit it for authoring convenience.
        if "action" not in shot:
            shot["action"] = "hold the closing composition steadily"
        story["shots"].append(shot)
    used = {beat["id"] for beat in spec["beats"]}; closing_id = "end"; suffix = 2
    while closing_id in used: closing_id = f"end_{suffix}"; suffix += 1
    cta = spec.get("cta") or {}; last = spec["beats"][-1]
    story["shots"].append({"id": closing_id, "panel": cta.get("panel") or f"the closing composition of {last['panel']}, held still"})
    if spec.get("allow_text"): warnings.append("spec.allow_text is ignored: compiled stories always set allow_text to false so captions remain editable")
    cues = []
    captions = spec.get("captions") or {}
    for index, (beat, timing) in enumerate(zip(spec["beats"], resolved)):
        text = beat.get("caption") or (captions.get("hook") if index == 0 else None)
        if text: cues.append({"start": timing["start"], "end": timing["end"], "text": text, "role": "hook" if index == 0 else "caption", "position": "center" if index == 0 else "bottom", "style": captions.get("style", "bold")})
    if cta.get("text"):
        final = resolved[-1]; cues.append({"start": final["start"], "end": final["end"], "text": cta["text"], "role": "cta", "position": "center", "style": captions.get("style", "bold")})
    disclosure = spec.get("disclosure")
    if disclosure:
        cues.append({"start": 0.0, "end": float(spec["duration"]), "text": disclosure if isinstance(disclosure, str) else DISCLOSURE_DEFAULT, "role": "disclosure", "position": [0.5, 0.04], "style": captions.get("style", "bold")})
    return {"story": story, "cues": cues, "beats": resolved, "safe": safe_zone(spec["platform"]), "platform": spec["platform"], "format": spec["format"], "total_duration": spec["duration"], "warnings": warnings}


def compile_board_prompt_extra(spec: dict) -> str:
    product = spec.get("product") or {}
    if not product.get("refs"): return ""
    name = product.get("name", "the product")
    text = (f"The product shown in the reference photographs is {name}. Reproduce it exactly as photographed wherever it appears: identical shape, proportions, colour, finish, and the exact label artwork and lettering. Do not redesign the packaging, do not invent or translate any wording on it, and do not substitute a similar product.")
    if product.get("palette"): text += f" Keep the surrounding palette anchored to {product['palette']}."
    return text


def retime_cues(cues: list[dict], beats: list[dict], actual: list[float]) -> list[dict]:
    """Re-time cues from the PLANNED beat durations onto the clips that actually came back.

    Video models do not return the duration you asked for. H3 answers a 6s request with
    6.583s, so a five-beat ad planned at 30s arrives as ~32.9s — and every cue after the
    first drifts later and later against the footage it belongs to, with the CTA landing
    around three seconds before the shot it is captioning. The plan is a budget, not a
    timeline.

    Each cue is mapped piecewise-linearly from its planned beat window onto that beat's
    real window, so a caption still starts and ends with its own shot. A cue that spans
    the whole film (the disclosure) stretches to the whole real film, because it is
    clamped by the first and last beat like any other.
    """
    if not beats or not actual:
        return [dict(cue) for cue in cues]
    if len(actual) != len(beats):
        raise AdSpecError(
            f"retime_cues: {len(actual)} clip duration(s) for {len(beats)} beat(s)\n"
            "  Every beat must have a clip before the cues can be re-timed — run `ad shots`."
        )

    edges = [0.0]
    for seconds in actual:
        edges.append(edges[-1] + float(seconds))

    def remap(t: float) -> float:
        for index, beat in enumerate(beats):
            span = beat["end"] - beat["start"]
            if t < beat["end"] or index == len(beats) - 1:
                fraction = 0.0 if span <= 0 else (t - beat["start"]) / span
                fraction = min(max(fraction, 0.0), 1.0) if index < len(beats) - 1 else max(fraction, 0.0)
                real = edges[index] + fraction * (edges[index + 1] - edges[index])
                return min(max(real, 0.0), edges[-1])
        return edges[-1]

    out = []
    for cue in cues:
        moved = dict(cue)
        moved["start"] = round(remap(float(cue["start"])), 3)
        moved["end"] = round(remap(float(cue["end"])), 3)
        if moved["end"] <= moved["start"]:
            moved["end"] = round(min(moved["start"] + 0.2, edges[-1]), 3)
        out.append(moved)
    return out


def estimate(plan: dict, per_second: float, board_cost: float) -> dict:
    clips = len(plan["story"]["shots"]) - 1; seconds = sum(beat["duration"] for beat in plan["beats"])
    clip_cost = round(seconds * per_second, 4)
    return {"clips": clips, "clip_seconds": seconds, "clips_cost": clip_cost, "board_cost": round(board_cost, 4), "total": round(clip_cost + board_cost, 4)}
