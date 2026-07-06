# Phase 1 Reconnaissance — Triage List

Roadmap item 7 output. Cheap AGOL-only existence check per county — no
field verification, no describe_layer of individual layers. That's
Phase 3 (roadmap item 9). This file is the baseline handed to Phase 3;
Phase 3 must extend it with direct Property Appraiser + county GIS URL
checks before writing counties off (see roadmap item 9's own note).

**Universe:** 65 statute-eligible FL counties (all 67 minus Miami-Dade
and Broward, which exceed the 1.75M cap in s. 163.3164(4)(f), F.S.).

**Already wired, skipped here:** the 7 counties currently
`confirmed_live=True` in [`app/county_registry.py`](../app/county_registry.py):
Hillsborough, Pasco, Brevard, Volusia, St. Johns, Nassau, Osceola.

**Triaged this pass:** 58 counties.

**Method:** ArcGIS Online item search with a tight match rule (title
must contain the county name AND "parcel" or "cadastr"). Script at
[`phase1_recon.py`](phase1_recon.py); re-run any time to regenerate.

**Caveat before acting on this:** "none" does NOT mean "no service
exists." It means "nothing indexed on AGOL under standard
parcel/cadastral titles." Big counties known to have real GIS
(Alachua, Marion, Polk, Duval, Monroe) all land in "none" or
"unclear" here because their services aren't published to AGOL under a
title Phase 1's tight rule catches. See roadmap item 9's re-check
list.

## LIVE — 10 counties (priority-1 for Phase 3)

| County | Service title | Owner | URL |
|---|---|---|---|
| Citrus | Citrus County FL Parcels | stevenscyphers | `services1.arcgis.com/5hzvezV1fsP5byjX/arcgis/rest/services/Citrus_County__FL_Parcels/FeatureServer` |
| Collier | Collier County Parcels | CollierCountyAGOL | `services2.arcgis.com/SlIq32SqARUHIhSx/arcgis/rest/services/Parcels/FeatureServer` |
| Gadsden | Parcels - Gadsden & Wakulla | cakee | `cotinter.leoncountyfl.gov/cotinter/rest/services/Vector/COT_OverlayParcels_OtherServiceAreas_D_WM/MapServer` |
| Glades | Glades Parcels42023 | sbcall | `services6.arcgis.com/90Aakxb3SLGcQGor/arcgis/rest/services/Glades_Parcels2020/FeatureServer` |
| Hendry | Hendry County Parcels | smccormick@hendryfla.net | `services7.arcgis.com/8l7Qq5t0CPLAJwJK/arcgis/rest/services/Hendry_County_Parcels/FeatureServer` |
| Lee | Lee County Parcels | LeeCountyFLGIS | `services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/services/Lee_County_Parcels/FeatureServer` |
| Leon | Vacant Parcels Tallahassee-Leon County | cakee | `intervector.leoncountyfl.gov/intervector/rest/services/MapServices/TLC_OverlayParnal_D_WM/MapServer/0` |
| Okeechobee | Parcels_Okeechobee | swade_geo_comm | `services.arcgis.com/mq0BGE5kHpm8mHFz/arcgis/rest/services/Parcels_Okeechobee/FeatureServer` |
| Pinellas | Pinellas_Parcels | PinellasCountyGIS | `services.arcgis.com/f5HgUpxURgEzTccH/arcgis/rest/services/Pinellas_Parcels_view/FeatureServer` |
| Wakulla | Parcels - Gadsden & Wakulla | cakee | (same URL as Gadsden — one shared Leon-hosted layer) |

Note: **Gadsden + Wakulla share a single Leon-hosted overlay layer**, so
that's one integration effort in Phase 3, not two.

## UNCLEAR — 21 counties (AGOL had a hit but it doesn't tightly match)

Top AGOL candidate for each. Most are visibly false positives (Vermont's
"Indian River," Montana's "Lake"), but a few (Palm Beach, Sarasota, Manatee,
Orange, Seminole, St. Lucie) are known real-GIS counties where AGOL just
didn't surface a parcel-titled service — those need direct-URL checks in
Phase 3, not to be written off from this list.

| County | Top AGOL candidate | Owner |
|---|---|---|
| Baker | Zoning designations Baker and Nassau Counties | CGRUSER_USG |
| Bay | FL _ BAY HARBOR ISLANDS_WFL1 | patrick.corley_mark_43 |
| Calhoun | Calhoun_FLUM2 | ARPCmaps |
| Charlotte | Coastal Flooding Hazard_WFL1 | gregory.guannel_uvigeocas |
| Duval | GWJax Base Layer GDB | LawrenceGWMKE |
| Escambia | ESCAMBIA_Coastal_Data | Rachel_STC |
| Flagler | Flagler Beach Flooding_WFL1 | cmiller_mckimcreed |
| Gulf | GULF_Coastal_Data | Rachel_STC |
| Hernando | HernandoBuilders | hcopropappr |
| Indian River | Delineated Floodplains on the Indian River in Pawlet_ VT_WFL1 | vrasmuss@uvm.edu_UVM |
| Lake | Basemap_MT_Flood (Montana) | CWB789@mt.gov_montana |
| Manatee | USACE SPGP V-R1 Regulatory Permit Review, SJRWMD | SJRWMDGeospatialSolutions |
| Martin | MARTIN AND PALM BEACH REFERENCE LAYERS | Jennifer_BAMBL |
| Okaloosa | OKALOOSA_Coastal_Data | Rachel_STC |
| Orange | 22_213 Pasco County FL Orange Belt Trail Study_WFL1 | alta_organization |
| Palm Beach | MARTIN AND PALM BEACH REFERENCE LAYERS | Jennifer_BAMBL |
| Sarasota | Sarasota National CDD_WFL1 | FelipeLemus |
| Seminole | Longwood Subarea Study Base Layers | Andrea.Sherman@hdrinc.com_HDR |
| St. Lucie | MARTIN AND PALM BEACH REFERENCE LAYERS | Jennifer_BAMBL |
| Sumter | Sumter Florence Rail Trail Property Ownership_WFL1 | duncan.watts_bmi |
| Walton | EnerGov_Backup | laikevin |

## NONE — 27 counties (no matching AGOL item under Phase 1's search)

**Not equivalent to "no service exists."** Many of these host their own
on-prem ArcGIS Server or use a Property Appraiser subdomain not indexed
here. Phase 3 needs per-county direct checks (roadmap item 9's re-check
list explicitly names **Alachua, Marion, Polk, Duval, and Monroe** as
known-significant counties that must be re-checked before write-off).

Alachua, Bradford, Clay, Columbia, DeSoto, Dixie, Franklin, Gilchrist,
Hamilton, Hardee, Highlands, Holmes, Jackson, Jefferson, Lafayette,
Levy, Liberty, Madison, Marion, Monroe, Polk, Putnam, Santa Rosa,
Suwannee, Taylor, Union, Washington.
