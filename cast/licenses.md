# Cast bible — licensing record

Every asset the cast bible renders from, with source, license, and commercial-use status.
Verified 2026-08-08. Men and Women packs: against the bundled `License.txt` in each download.
Furniture pack: the download (Blends folder only) carries no License.txt — verified instead
against quaternius.com/packs/furniture.html, which states CC0 and links the exact Drive folder
`fetch_assets.sh` fetches. Re-verify after any re-download.

| Asset pack | Used for | Source | License | Commercial use |
|---|---|---|---|---|
| Quaternius **Animated Men** (Male_Suit, Male_Casual, Male_LongSleeve, Male_Shirt + 12 `Man_*` actions) | omar, khalid, hamdan + the shared pose vocabulary | quaternius.com → Google Drive folder `17LibivOaUidsQhSkcxP3YYvDr0n7wIwu` | **CC0 1.0** (public domain, per bundled License.txt) | Yes, unrestricted, no attribution required |
| Quaternius **Animated Women** (Female_Casual, Female_Dress, Female_TankTop, Female_Alternative + 12 `Female_*` actions) | sara, noor + the shared pose vocabulary | quaternius.com → Google Drive folder `1c13R--fMqdR6r2MRlcKKsbPky0__T-yJ` | **CC0 1.0** (public domain, per bundled License.txt) | Yes, unrestricted, no attribution required |
| Quaternius **Furniture pack** (Chair, Table, Plant, BookCaseLargeBooks) | classroom + desk sets | quaternius.com → Google Drive folder `1CLWStkb7cipC1ZdTunYJXKVqEVwtXWXK` | **CC0 1.0** (per quaternius.com/packs/furniture.html — no License.txt in the Blends-only download) | Yes, unrestricted, no attribution required |
| Xane Graphics **Lollipop Characters V1.2.4** (Mike, Farida, Femi + per-character Wardrobe) | rigged hero cast for scenario panels (Day-5 v3 onward) | purchased (Blender Market, Xane Graphics) | **GPL v2+** (per the GPL license block in the bundled `Addon/Lollipop_Characters.py`; the zip carries no separate license text for the .blend files) | Renders are original artwork and ours to use; the .blend files themselves stay GPL — do not redistribute them in the repo, fetch from your own purchase |

Everything else in a rendered panel (rooms, boards, racks, laptops, LEDs) is procedural box
geometry authored in the set specs — ours, no license involved. Renders produced from these
packs are original works; the words-never-in-pixels rule means no third-party fonts enter the
images either.

**Not in the bible:** Poly Haven HDRIs (not used) and the UAL animation library (downloaded
during the spike, CC0, currently unused by any set).

Lollipop headless-append verification (2026-08-08): characters append cleanly with `cs_*`
rig-widget meshes excluded; garments fit via the weight-transfer recipe in `rigged-cast.md`;
every panel render passes through a memory guard after a fur particle system on `Femi - Jacket`
OOM-froze an 8 GB WSL2 VM. Strip `PARTICLE_SYSTEM`/`CLOTH`/`SOFT_BODY` before rendering.

Attribution, though not required by CC0, is a courtesy: models and animations by
[Quaternius](https://quaternius.com).
