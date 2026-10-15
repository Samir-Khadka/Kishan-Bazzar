# Kisan Bazar — Crop Calendar Reference (expanded)

Purpose: widen the advisory rule table (`crop_calendar_rules` in the backend
schema, `RULES` in the app prototype) from its original 15 illustrative
entries toward full BS-year coverage across all five elevation bands.

**Read the honesty note below before using any of this for real advice.**

---

## What's actually verifiable vs. what still needs sign-off

I searched for NARC's and DoA's published crop calendars while building
this. What's confirmed from real, citable sources:

- Nepal's elevation bands and which broad crop groups suit each are
  well-documented: the tropical zone below 1,000m suits fruits and general
  cropping, the subtropical zone (1,000–2,000m) is the most farmed and
  suits rice, maize, millet and wheat, and the temperate zone (2,000–3,000m)
  suits cold-tolerant crops — with mountain farming above that dominated by
  a potato/barley/buckwheat system on steep, fragmented, low-fertility land
  typically yielding one harvest a year.
- The Terai supports high crop intensification — up to three crops a year
  on the same land, which is *why* rotations there (paddy → wheat/lentil →
  short-season vegetable) are agronomically standard, not incidental.
- Hill irrigated khet grows rice up to roughly 1,800m, followed by winter
  wheat, with potato and vegetables increasingly displacing wheat because
  they pay better; unirrigated hill bari runs maize, millet, mustard, and
  a long tail of fruit trees and legumes.

What I could **not** verify from a citable source: the exact BS-month
sowing windows per crop, per district. NARC and DoA publish these as
tables inside PDFs and, in the sources I found, as embedded images —
not as text I can read or quote precisely. The rows below are my
synthesis of the general agronomic pattern above, cross-checked against
what was already in the prototype's rule table, **not a transcription of
an official calendar**. Treat every `sow_months_bs` value below as a
draft needing confirmation from NARC, a district Agriculture Knowledge
Centre, or DoA's provincial office before it reaches a real farmer.

This matches the constraint already written into the schema
(`crop_calendar_rules.source` and `.reviewed_at` are mandatory, non-null
columns) — the table structure assumes exactly this review step happens
before any row goes live.

---

## Expanded calendar, by zone

BS month index: 0=Baisakh, 1=Jestha, 2=Asar, 3=Shrawan, 4=Bhadau, 5=Asoj,
6=Kartik, 7=Mangsir, 8=Push, 9=Magh, 10=Falgun, 11=Chaitra.

### Terai (60–300m) — up to 3 crops/year on the same land

| Crop | Land | Sow (BS) | Duration | Note |
|---|---|---|---|---|
| Paddy (main) | khet | Asar–Shrawan (2–3) | ~140d | The anchor crop; almost everything else in the Terai rotation is planned around its harvest. |
| Wheat | khet, bari | Kartik–Mangsir (6–7) | ~120d | Follows paddy directly; increasingly displaced by potato/vegetables where irrigation allows a third crop. |
| Lentil / mustard | bari | Kartik–Mangsir (6–7) | ~100–110d | Rainfed rotation after paddy; lentil fixes nitrogen for the next paddy cycle. |
| Maize (spring) | bari | Chaitra–Baisakh (11–0) | ~100d | Pre-monsoon planting on drier bari land. |
| Potato | khet, bari, balaute | Kartik–Push (6–8) | ~90–100d | The cash-crop displacement of winter wheat mentioned above. |
| Onion | khet | Kartik–Mangsir (6–7) | ~130d | Needs staggered sales or storage — floods the market if everyone harvests together. |
| Jute (east Terai) | bari | Baisakh–Jestha (0–1) | ~120d | Regional specialty (Jhapa/Morang); not general-purpose. |
| Sugarcane | bari | Chaitra–Baisakh (11–0) | 10–12 months | Long-cycle cash crop, needs a contracted mill buyer, not spot-market sale. |

### Inner Terai (300–600m) — Terai patterns, slightly compressed calendar

Broadly follows the Terai table above, shifted 2–3 weeks earlier in the BS
calendar due to lower elevation warming faster; vegetable rotations
(tomato, cauliflower) are more prominent here than pure cereal rotation
because of proximity to hill and Terai markets both.

### Mid-hills (600–2,500m) — the most agriculturally complex zone

| Crop | Land | Sow (BS) | Duration | Note |
|---|---|---|---|---|
| Paddy | khet (up to ~1800m) | Asar–Shrawan (2–3) | ~145d | Terraced, irrigated where water is diverted from streams. |
| Wheat | khet | Kartik–Mangsir (6–7) | ~120d | Follows paddy; being displaced by potato and vegetables in market-accessible areas, per the sourced pattern above. |
| Maize | bari | Chaitra–Baisakh (11–0) | ~130d | The staple bari crop of the hills. |
| Finger millet (kodo) | bari | Asar–Shrawan (2–3), often intercropped with maize | ~150d | Traditional food-security crop, low market value but drought-tolerant. |
| Mustard | bari, khet | Kartik–Mangsir (6–7) | ~100d | Oil crop, ready by Magh. |
| Potato | khet, bari, balaute | Asoj–Mangsir (5–7) | ~95d | Multiple planting windows possible at this elevation range. |
| Cauliflower / cabbage | khet, bari | Bhadau–Mangsir (4–7), staggered nursery | ~75d | High-value off-season vegetable; co-op pooling matters most here because plots are small. |
| Pea | khet, bari | Kartik–Mangsir (6–7) | ~85d | Winter cash crop for city markets. |
| Ginger | bari, pakho | Chaitra–Baisakh (11–0) | ~240d | Major cash crop in Salyan, Palpa; harvested the following Mangsir–Push. |
| Large cardamom | pakho, bari | Chaitra–Jestha (11–1), perennial | multi-year | Ilam/Panchthar/Taplevung specialty; needs shade cover, years to establish. |
| Coffee | pakho | Chaitra–Jestha (11–1), perennial | multi-year | Gulmi/Palpa/Syangja; needs 3+ years to first harvest. |
| Junar (sweet orange) | pakho, bari | Push–Magh (8–9) for saplings, perennial | 3–4 yrs to bear | Sindhuli/Ramechhap cash fruit. |

### High hills (2,500–3,500m) — potato/barley/buckwheat zone

Per the sourced description above: steep, less fertile, fragmented
holdings, typically one harvest a year.

| Crop | Land | Sow (BS) | Duration | Note |
|---|---|---|---|---|
| Potato | bari, pakho | Baisakh–Jestha (0–1) | ~120–150d | The dominant food and cash crop at this elevation. |
| Barley | bari, pakho | Baisakh (0) or Kartik (6) depending on micro-site | ~130d | Cold-tolerant staple. |
| Buckwheat | bari, pakho | Baisakh–Asar (0–2) | ~80d | Short-cycle, low-input; often a secondary or catch crop. |
| Apple | pakho | Push–Magh (8–9) for saplings, perennial | 3–4 yrs to bear | Mustang/Jumla; needs cold-hour accumulation, not viable below this band. |

### Mountain (3,500m+) — minimal cropping, mostly pastoral

| Crop | Land | Sow (BS) | Duration | Note |
|---|---|---|---|---|
| Naked barley | bari | Baisakh (0) | ~140d | Highest-elevation viable cereal in most inhabited valleys. |
| Buckwheat | bari | Baisakh–Jestha (0–1) | ~85d | Short growing season demands early sowing right after snowmelt. |
| Potato (limited) | bari | Baisakh–Jestha (0–1) | ~130d | Only in the lower, warmer pockets of this band (e.g. lower Mustang valleys). |

Above roughly 3,800–4,000m, cropping gives way almost entirely to yak/chauri
pastoralism and transhumance — the app's advisory screen should say so
plainly rather than force a crop recommendation where none is agronomically
sound.

---

## What to do with this before it reaches `crop_calendar_rules`

1. Take this table to a NARC contact point or a provincial DoA office —
   ideally per-district, since "mid-hills" spans huge variation between,
   say, Kaski and Bajura.
2. Replace every `source` field in the SQL migration's seed data
   (currently placeholder strings like `'NARC 2081 crop calendar v3'`)
   with the actual document/contact that confirmed each row.
3. Set `reviewed_at` honestly to when that confirmation happened, and
   build the re-review cadence into an ops calendar — these calendars
   drift with changing rainfall patterns, and a stale row is worse than
   no row, because it carries false confidence.
4. Until that review happens, the app should visibly mark advisory
   content as provisional (a small "अझै NARC ले पुष्टि गरेको छैन — draft
   pending confirmation" tag), matching how the prototype already treats
   advisory notes as options with tradeoffs rather than instructions.
