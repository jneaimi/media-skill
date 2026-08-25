# Vendored: xfade-easing

Source: https://github.com/scriptituk/xfade-easing (v3.6.6)
Author: Raymond Luckhurst <raymond@scriptit.uk>
License: MIT — see LICENSE in this directory.

## What is vendored, and why

Two pre-generated expression files, copied verbatim:

- `eased-transitions-yuv420p-inline.txt` — 106 transition expressions, 50 of which are
  ports of the GL Transitions GLSL library.
- `xfade-easings-inline.txt` — 43 easing curves (Penner, plus squareroot/cuberoot/
  flipelastic/flipback).

These are plain FFmpeg expression strings for `xfade=transition=custom:expr=...`. They need
**no custom FFmpeg build** — which is the whole reason this project was chosen over
`ffmpeg-gl-transition` and `ffmpeg-gl-effects`, both of which require compiling FFmpeg with
`--enable-opengl` plus GLEW and glfw. A skill that ships publicly cannot ask that of a user.

## How they compose

An easing expression assigns `st(0, …)`; every transition expression reads it back as `ld(0)`.
So an eased transition is the two joined by a semicolon, easing first:

    expr = EASINGS["CUBIC-IN-OUT"] + ";" + TRANSITIONS["GL_DOORWAY"]

`LINEAR` (`st(0,P)`) is the identity easing and the default.

## The cost

Expressions use `st()`/`ld()` state shared between slice threads, so any filtergraph using them
must pass `-filter_complex_threads 1`. Upstream benchmarks it at roughly 6s versus 0.5s per
720p transition against the native C build. At sub-second transition durations on a handful of
cuts that is seconds per render, which is why the expression route is preferred here over
patching FFmpeg.

The 58 transitions built into FFmpeg's own `xfade` filter are unaffected and stay the fast path.
