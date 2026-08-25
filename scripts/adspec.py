"""Compile social-ad specs into the established storyboard pipeline.

Ads are deliberately lowered into ordinary stories so the already-proven board, bridge,
and assembly chain remains unchanged.  Platform safe zones reserve the pixels covered by
social UI, keeping later-burned copy readable.  This module is intentionally stdlib-only:
it neither imports Pillow or ffmpeg nor makes network requests.
"""

from __future__ import annotations

import json
import sys
from difflib import get_close_matches
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


def beat_panels(beat: dict, where: str = "beat") -> list[str]:
    """The panels this beat is drawn from — one per clip it becomes.

    A beat is one clip by default, and one clip is capped at the video model's maximum
    (15s for H3). A beat that needs longer is written as `panels: [...]`, and its
    duration is split across them. That is not a workaround for the cap; it is what makes
    a long beat WORTH the seconds. The chain bridges panel i to panel i+1, so two panels
    is a shot that travels somewhere, while one panel stretched to 20s is a video model
    inventing 20 seconds of unaided motion — which is exactly where H3 drifts.
    """
    panel, panels = beat.get("panel"), beat.get("panels")
    if panels is not None:
        if panel is not None:
            raise AdSpecError(f"{where}: set panel OR panels, not both — panels is the multi-clip form")
        if not isinstance(panels, list) or len(panels) < 2:
            raise AdSpecError(f"{where}.panels must be a list of at least 2 panel descriptions; use panel for a single clip")
        for i, text in enumerate(panels):
            if not isinstance(text, str) or not text:
                raise AdSpecError(f"{where}.panels[{i}] must be a non-empty string")
        return list(panels)
    if not isinstance(panel, str) or not panel:
        raise AdSpecError(f"{where}.panel is required")
    return [panel]


def beat_actions(beat: dict, count: int) -> list[str | None]:
    """One action per clip. `actions` overrides `action`; `action` repeats across clips."""
    actions = beat.get("actions")
    if isinstance(actions, list) and actions:
        return [actions[i] if i < len(actions) else actions[-1] for i in range(count)]
    return [beat.get("action")] * count


def validate_transition(value, where: str) -> None:
    """Shape-check a transition. Names are checked against the catalogue, not against
    what THIS ffmpeg build can render — a spec has to validate on a machine with no
    ffmpeg installed, the same as every other pure function here."""
    if not isinstance(value, dict):
        raise AdSpecError(f"{where} must be an object like {{\"type\": \"fade\", \"duration\": 0.4}}")
    extras = sorted(set(value) - {"type", "duration", "easing"})
    if extras:
        raise AdSpecError(f"{where}: unknown key(s): {', '.join(extras)}")
    name = value.get("type")
    if not isinstance(name, str) or not name:
        raise AdSpecError(f"{where}.type is required")
    seconds = value.get("duration", 0.4)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        raise AdSpecError(f"{where}.duration must be a positive number of seconds")
    try:
        import transitions as _t
    except ImportError:
        return
    try:
        _t.resolve(name, float(seconds), value.get("easing"))
    except Exception as exc:
        raise AdSpecError(f"{where}: {exc}") from None


def validate_effects(value, where: str) -> None:
    """Shape-check a beat's effect list against the registry, not against availability."""
    if not isinstance(value, list):
        raise AdSpecError(f"{where} must be a list of effect objects")
    try:
        import effects as _e
        known = set(_e.EFFECTS)
    except ImportError:
        known = None
    for i, item in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise AdSpecError(f"{at} must be an object like {{\"name\": \"zoom_punch\", \"at\": 1.0}}")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise AdSpecError(f"{at}.name is required")
        if known is not None and name not in known:
            close = get_close_matches(name, sorted(known), n=4)
            hint = f" — did you mean {', '.join(close)}?" if close else f" — known: {', '.join(sorted(known))}"
            raise AdSpecError(f"{at}.name {name!r} is not a known effect{hint}")


def validate_overlays(value, where: str) -> None:
    """Shape-check the film's graphic overlays. Timing is checked against the film in
    compile_to_story, where the duration is known."""
    if not isinstance(value, list):
        raise AdSpecError(f"{where} must be a list of overlay objects")
    for i, item in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(item, dict):
            raise AdSpecError(f"{at} must be an object")
        extras = sorted(set(item) - {"asset", "badge", "arrow", "start", "end", "beat", "anchor",
                                     "offset", "scale", "opacity", "rotation", "fade_in",
                                     "fade_out", "tracks", "id", "style", "fill", "text"})
        if extras:
            raise AdSpecError(f"{at}: unknown key(s): {', '.join(extras)}")
        if not (item.get("asset") or item.get("badge") or item.get("arrow")):
            raise AdSpecError(f"{at}: needs one of asset (a PNG path), badge (text), or arrow (a direction)")
        if "beat" in item and ("start" in item or "end" in item):
            raise AdSpecError(f"{at}: set beat OR start/end, not both — beat anchors the overlay to that beat's window")
        if "beat" not in item and not ("start" in item and "end" in item):
            raise AdSpecError(f"{at}: needs either beat, or both start and end")


def validate_ad_spec(spec: dict) -> list[str]:
    if not isinstance(spec, dict):
        raise AdSpecError("spec: top level must be an object")
    if spec.get("version") != 1:
        raise AdSpecError("spec.version must be 1")
    if "kind" in spec and spec["kind"] != "ad":
        raise AdSpecError("spec.kind must be 'ad'")
    known = {"version", "kind", "title", "format", "platform", "duration", "model", "provider", "resolution", "style", "product", "actor", "cast", "beats", "captions", "cta", "disclosure", "board", "allow_text", "transition", "overlays"}
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
    beat_keys = {"id", "panel", "panels", "action", "actions", "camera", "say", "lang", "caption", "sound", "duration", "ending", "allow_text", "transition", "effects"}
    for index, beat in enumerate(beats):
        where = f"spec.beats[{index}]"
        if not isinstance(beat, dict): raise AdSpecError(f"{where} must be an object")
        extras = sorted(set(beat) - beat_keys)
        if extras: raise AdSpecError(f"{where}: unknown key(s): {', '.join(extras)}")
        ident = beat.get("id")
        if not isinstance(ident, str) or not ident: raise AdSpecError(f"{where}.id is required")
        if ident in seen: raise AdSpecError(f"{where}.id {ident!r} is duplicated")
        seen.add(ident); ids.append(ident)
        beat_panels(beat, where)
        if index < len(beats) - 1 and not beat.get("action") and not beat.get("actions"): raise AdSpecError(f"{where}.action is required")
        if "duration" in beat and (type(beat["duration"]) is not int or beat["duration"] <= 0): raise AdSpecError(f"{where}.duration must be a positive integer")
        if index == 0 and "transition" in beat: raise AdSpecError(f"{where}.transition: the first beat has nothing before it to cut from — put the transition on the beat it cuts INTO")
        if "transition" in beat: validate_transition(beat["transition"], f"{where}.transition")
        if "effects" in beat: validate_effects(beat["effects"], f"{where}.effects")
    roles = fmt["roles"]
    if fmt_name != "custom" and (len(beats) != len(roles) or tuple(ids) != roles):
        raise AdSpecError(f"spec.beats: format {fmt_name!r} expects ids {', '.join(roles)} — got {', '.join(ids)}")
    allocation_format = fmt if roles else {
        "roles": tuple(ids), "weights": tuple(1 / len(ids) for _ in ids),
    }
    # Validate the floor before any image or video work starts; a five-beat 12-second
    # request cannot become valid later in the pipeline.
    allocate_beats(allocation_format, duration,
                   overrides=[beat.get("duration") for beat in beats],
                   clips=[len(beat_panels(beat, f"spec.beats[{i}]")) for i, beat in enumerate(beats)])
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
    if "transition" in spec: validate_transition(spec["transition"], "spec.transition")
    if "overlays" in spec:
        validate_overlays(spec["overlays"], "spec.overlays")
        beat_ids = set(ids)
        for i, item in enumerate(spec["overlays"]):
            ref = item.get("beat")
            if ref is not None and ref not in beat_ids:
                raise AdSpecError(f"spec.overlays[{i}].beat {ref!r} is not a beat id — have: {', '.join(ids)}")
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


def allocate_beats(fmt: dict, total: int, *, minimum: int = 4, maximum: int = 15, overrides: list[int | None] | None = None, clips: list[int] | None = None) -> list[int]:
    """Apportion `total` seconds across the format's beats.

    `minimum` and `maximum` are the VIDEO MODEL's per-clip limits, so a beat drawn from
    several panels scales both by its clip count: a two-panel beat can run to 30s because
    it is rendered as two clips of at most 15s each, not as one impossible 30s request.
    """
    roles, weights = fmt["roles"], fmt["weights"]; count = len(roles)
    if count == 0: raise AdSpecError("format.roles must contain at least one beat")
    if overrides is None: overrides = [None] * count
    if len(overrides) != count: raise AdSpecError("overrides must have one entry per beat")
    if clips is None: clips = [1] * count
    if len(clips) != count: raise AdSpecError("clips must have one entry per beat")
    if any(type(n) is not int or n < 1 for n in clips): raise AdSpecError("clips entries must be positive integers")
    beat_floors = [minimum * n for n in clips]
    beat_ceils = [maximum * n for n in clips]
    if total < sum(beat_floors): raise AdSpecError(f"duration {total}s cannot cover {count} beats at {minimum}s minimum per clip — needs at least {sum(beat_floors)}s")
    result = [0] * count
    for index, value in enumerate(overrides):
        if value is not None:
            if type(value) is not int or value < beat_floors[index] or value > beat_ceils[index]:
                extra = f" ({clips[index]} clips)" if clips[index] > 1 else ""
                raise AdSpecError(f"overrides[{index}] must be between {beat_floors[index]} and {beat_ceils[index]}{extra}")
            result[index] = value
    free = [i for i, value in enumerate(overrides) if value is None]
    remaining = total - sum(result)
    if remaining < sum(beat_floors[i] for i in free): raise AdSpecError(f"duration {total}s cannot cover unpinned beats at {minimum}s minimum per clip")
    if not free: return result
    # Reserve the floor, then apportion the remaining seconds by Hare's largest remainders.
    pool = remaining - sum(beat_floors[i] for i in free)
    for index in free:
        result[index] = beat_floors[index]
    while pool:
        active = [index for index in free if result[index] < beat_ceils[index]]
        if not active:
            break
        total_weight = sum(weights[index] for index in active)
        shares = {index: pool * weights[index] / total_weight for index in active}
        floors = {index: min(beat_ceils[index] - result[index], int(shares[index])) for index in active}
        used = sum(floors.values())
        for index in active:
            result[index] += floors[index]
        pool -= used
        if not pool:
            break
        candidates = [index for index in active if result[index] < beat_ceils[index]]
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


def split_duration(total: int, count: int, *, minimum: int = 4, maximum: int = 15) -> list[int]:
    """Split one beat's seconds across the clips it is drawn from, as evenly as possible.

    Remainder seconds go to the earliest clips, which is the right bias: the opening clip
    of a beat carries the movement that establishes it.
    """
    if count < 1:
        raise AdSpecError("split_duration: count must be at least 1")
    if total < count * minimum or total > count * maximum:
        raise AdSpecError(f"split_duration: {total}s cannot be split across {count} clip(s) at {minimum}-{maximum}s each")
    base, extra = divmod(total, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def beat_timing(durations: list[int], ids: list[str], roles: tuple[str, ...] | list[str], clips: list[int] | None = None) -> list[dict]:
    cursor = 0.0; resolved = []
    if clips is None: clips = [1] * len(durations)
    clip_index = 0
    for index, (duration, ident) in enumerate(zip(durations, ids)):
        end = cursor + float(duration)
        count = clips[index]
        parts = split_duration(duration, count)
        resolved.append({"id": ident, "index": index, "duration": duration, "start": cursor, "end": end,
                         "role": roles[index] if index < len(roles) else ident,
                         "clips": count, "clip_durations": parts, "first_clip": clip_index})
        clip_index += count
        cursor = end
    return resolved


def compile_to_story(spec: dict) -> dict:
    warnings = validate_ad_spec(spec)[:]
    fmt, platform = format_for(spec["format"]), platform_for(spec["platform"])
    allocation_format = fmt if fmt["roles"] else {
        "roles": tuple(beat["id"] for beat in spec["beats"]),
        "weights": tuple(1 / len(spec["beats"]) for _ in spec["beats"]),
    }
    clip_counts = [len(beat_panels(beat, f"spec.beats[{i}]")) for i, beat in enumerate(spec["beats"])]
    durations = allocate_beats(allocation_format, spec["duration"], overrides=[beat.get("duration") for beat in spec["beats"]], clips=clip_counts)
    resolved = beat_timing(durations, [beat["id"] for beat in spec["beats"]], fmt["roles"], clip_counts)
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
        panels = beat_panels(beat)
        actions = beat_actions(beat, len(panels))
        # A beat is one shot unless it names several panels, in which case it is that many
        # shots and its seconds are split across them. The dialogue and the closing-action
        # fallback belong to the FIRST shot only: a line of speech is said once, not once
        # per clip the beat happens to be rendered from.
        for part, (panel_text, panel_action, seconds) in enumerate(
                zip(panels, actions, timing["clip_durations"])):
            shot = {key: beat[key] for key in ("camera", "ending") if key in beat}
            shot["id"] = beat["id"] if part == 0 else f"{beat['id']}_{part + 1}"
            shot["panel"] = panel_text
            if panel_action:
                shot["action"] = panel_action
            shot["duration"] = seconds
            if part == 0:
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
    used = {shot["id"] for shot in story["shots"]}; closing_id = "end"; suffix = 2
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
    transitions = transition_plan(spec, resolved)
    effects = effect_plan(spec, resolved)
    overlays, overlay_warnings = overlay_plan(spec, resolved)
    warnings.extend(overlay_warnings)
    cut_seconds = round(sum(t["duration"] for t in transitions if t), 3)
    if cut_seconds:
        warnings.append(f"transitions overlap {cut_seconds}s in total, so the finished film is that much shorter than its {spec['duration']}s of footage")
    return {"story": story, "cues": cues, "beats": resolved, "safe": safe_zone(spec["platform"]),
            "platform": spec["platform"], "format": spec["format"],
            "total_duration": spec["duration"], "transitions": transitions,
            "effects": effects, "overlays": overlays, "warnings": warnings}


def transition_plan(spec: dict, resolved: list[dict]) -> list[dict | None]:
    """One entry per CLIP: the transition cutting into it, or None for a hard cut.

    Two rules, and the second is the one that matters:

    - A beat's `transition` applies to the cut INTO that beat, so it lands on clip
      `first_clip`. `spec.transition` supplies the default for every beat that is silent.
    - **The clips WITHIN a beat never get one.** They are bridge cuts: clip i ends on
      exactly the frame clip i+1 begins on, which is what makes them invisible. Fading
      across a bridge cross-dissolves a frame with itself — it costs runtime and shows
      nothing. A transition is for a DELIBERATE discontinuity, and inside a beat there
      isn't one.
    """
    default = spec.get("transition")
    total_clips = sum(beat["clips"] for beat in resolved)
    plan: list[dict | None] = [None] * total_clips
    for index, (beat, timing) in enumerate(zip(spec["beats"], resolved)):
        if index == 0:
            continue  # nothing precedes the first clip
        chosen = beat.get("transition", default)
        if not chosen:
            continue
        entry = {"type": chosen["type"], "duration": float(chosen.get("duration", 0.4))}
        if chosen.get("easing"):
            entry["easing"] = chosen["easing"]
        plan[timing["first_clip"]] = entry
    return plan


def effect_plan(spec: dict, resolved: list[dict]) -> list[list[dict]]:
    """One list per CLIP: the effects to apply, with `at` rebased onto that clip.

    A beat's effects are authored against the BEAT's timeline. When the beat is one clip
    those are the same thing; when it is several, an effect at 12s belongs to the third
    clip at 2s, not to the first clip at 12s — where it would land past the end and
    silently never fire.

    An effect with no `at` is not a moment, it is a treatment of the whole shot —
    ken_burns, color_pop, a grade. Those apply to every clip of the beat, unchanged. Only
    a timed effect gets rebased, and only a timed effect can be filtered out for landing
    outside a given clip.
    """
    total_clips = sum(beat["clips"] for beat in resolved)
    plan: list[list[dict]] = [[] for _ in range(total_clips)]
    for beat, timing in zip(spec["beats"], resolved):
        cursor = 0.0
        for part, seconds in enumerate(timing["clip_durations"]):
            clip_start, clip_end = cursor, cursor + seconds
            for item in beat.get("effects") or []:
                if "at" not in item:
                    plan[timing["first_clip"] + part].append(dict(item))
                    continue
                at = float(item["at"])
                span = float(item.get("duration", 0.0))
                if at >= clip_end or (span and at + span <= clip_start):
                    continue
                if not span and not (clip_start <= at < clip_end):
                    continue
                rebased = dict(item)
                rebased["at"] = round(max(0.0, at - clip_start), 3)
                plan[timing["first_clip"] + part].append(rebased)
            cursor = clip_end
    return plan


def overlay_plan(spec: dict, resolved: list[dict]) -> tuple[list[dict], list[str]]:
    """Resolve each overlay's window onto the film timeline, and warn about the rest.

    `beat: "<id>"` is sugar for that beat's window, which is how an overlay stays
    attached to its shot when the beat allocation changes underneath it.
    """
    by_id = {beat["id"]: beat for beat in resolved}
    out, warnings = [], []
    film = float(spec["duration"])
    for index, item in enumerate(spec.get("overlays") or []):
        entry = dict(item)
        ref = entry.pop("beat", None)
        if ref is not None:
            timing = by_id[ref]
            entry["start"], entry["end"] = timing["start"], timing["end"]
        entry["start"], entry["end"] = float(entry["start"]), float(entry["end"])
        if entry["end"] <= entry["start"]:
            raise AdSpecError(f"spec.overlays[{index}]: end must be after start")
        if entry["end"] > film:
            warnings.append(f"spec.overlays[{index}] ends at {entry['end']}s, past the {film}s film")
        entry.setdefault("id", entry.get("badge") or entry.get("arrow") or f"overlay_{index + 1}")
        out.append(entry)
    return out, warnings


def compile_board_prompt_extra(spec: dict) -> str:
    product = spec.get("product") or {}
    if not product.get("refs"): return ""
    name = product.get("name", "the product")
    text = (f"The product shown in the reference photographs is {name}. Reproduce it exactly as photographed wherever it appears: identical shape, proportions, colour, finish, and the exact label artwork and lettering. Do not redesign the packaging, do not invent or translate any wording on it, and do not substitute a similar product.")
    if product.get("palette"): text += f" Keep the surrounding palette anchored to {product['palette']}."
    return text


def retime_cues(cues: list[dict], beats: list[dict], actual: list[float],
                transitions: list[dict | None] | None = None) -> list[dict]:
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

    `transitions` — one entry per CLIP, as `transition_plan` returns — accounts for the
    second way the timeline moves. xfade OVERLAPS its two inputs rather than inserting
    between them, so each transition removes its own duration from the finished film. Four
    0.4s cuts pull everything after the first shot 1.6s earlier, and captions re-timed
    against clip durations alone would sit that much late by the end. `actual` is a list of
    clip durations, so pass the whole per-clip list here even when beats are multi-clip.
    """
    if not beats or not actual:
        return [dict(cue) for cue in cues]

    # `actual` is per CLIP; beats may own several. Fold clips back into beat totals first
    # so the piecewise map still has one real window per beat.
    counts = [beat.get("clips", 1) for beat in beats]
    if len(actual) == sum(counts) and sum(counts) != len(beats):
        folded, cursor = [], 0
        for n in counts:
            folded.append(sum(float(s) for s in actual[cursor:cursor + n]))
            cursor += n
        actual = folded
    elif len(actual) != len(beats):
        raise AdSpecError(
            f"retime_cues: {len(actual)} clip duration(s) for {len(beats)} beat(s)"
            f" ({sum(counts)} clip(s))\n"
            "  Every beat must have a clip before the cues can be re-timed — run `ad shots`."
        )

    overlaps = [0.0] * len(actual)
    if transitions:
        cursor = 0
        for index, n in enumerate(counts):
            window = transitions[cursor:cursor + n] if cursor < len(transitions) else []
            overlaps[index] = sum(float(t["duration"]) for t in window if t)
            cursor += n

    edges = [0.0]
    for seconds, lost in zip(actual, overlaps):
        edges.append(edges[-1] + float(seconds) - lost)

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
