"""
County registry for the Enclave Scanner pilot.

CORRECTED 2026-07-03: the `fips` field on every entry below was wrong in
the prior version -- verified against the Florida Dept. of Revenue's
official County Number Map (floridarevenue.com/property/Documents/
CountyNumberMap.pdf) and the DOR's own NAL Users Guide. This is the field
used to filter the statewide cadastral layer's CO_NO attribute, so a wrong
value here silently returns zero or wrong-county results -- this was very
likely the root cause of prior scans not working.

Full official DOR county number table (all 67), for reference so nothing
gets re-guessed later:

  11 Alachua      23 Miami-Dade   35 Hardee       47 Leon         59 Osceola      71 Suwannee
  12 Baker        24 DeSoto       36 Hendry       48 Levy         60 Palm Beach   72 Taylor
  13 Bay          25 Dixie        37 Hernando     49 Liberty      61 Pasco        73 Union
  14 Bradford     26 Duval        38 Highlands    50 Madison      62 Pinellas     74 Volusia
  15 Brevard      27 Escambia     39 Hillsborough 51 Manatee      63 Polk         75 Wakulla
  16 Broward      28 Flagler      40 Holmes       52 Marion       64 Putnam       76 Walton
  17 Calhoun      29 Franklin     41 Indian River 53 Martin       65 St. Johns    77 Washington
  18 Charlotte    30 Gadsden      42 Jackson      54 Monroe       66 St. Lucie
  19 Citrus       31 Gilchrist    43 Jefferson    55 Nassau       67 Santa Rosa
  20 Clay         32 Glades       44 Lafayette    56 Okaloosa     68 Sarasota
  21 Collier       33 Gulf         45 Lake         57 Okeechobee   69 Seminole
  22 Columbia     34 Hamilton     46 Lee          58 Orange       70 Sumter

IMPORTANT -- verified vs. assumed (unchanged from prior pass, still true):
  - url, flu_field, jurisdiction_field, acreage_field: confirmed against
    the live layer's /query?f=pjson metadata response.
  - usb_values: NOT verified per-county. Only Hillsborough is confirmed
    to carry an explicit Urban Service Area boundary as a distinct
    layer. For the other six, "near USB" is approximated from FLUM
    category names (e.g. anything not Agricultural/Rural) until each
    county's actual USB layer is located and wired in.

GROUND-TRUTHED 2026-07-03 -- CO_NO filtering on the statewide cadastral
layer does not work, live query confirmed:
  - `DOR_UC` field/values ARE confirmed correct against live data:
    querying `DOR_UC>='050' AND DOR_UC<='069'` against
    STATEWIDE_CADASTRAL_URL returns real agricultural parcels with
    values like '052', '055', '059', '061' -- the ('050','069') fix
    holds.
  - BUT filtering that same layer by `CO_NO=<value>` (the field this
    file's `fips` values are meant for) times out every time -- 400
    after ~55s, or a real 504 Gateway Timeout, for EVERY predicate
    variant tried (equality, inequality, combined with DOR_UC, spatial
    envelope/polygon filter using the county's own boundary polygon).
    Root cause: CO_NO has no index on this hosted layer (confirmed via
    its own /query?f=pjson metadata -- only OBJECTID, Shape, Shape__Area,
    Shape__Length, and PARCEL_ID are indexed), and the query engine
    appears to do an early-exit sequential scan by physical row order
    (which tracks CO_NO ascending) rather than a real indexed/R-tree
    lookup -- confirmed by testing the identical spatial-filter query
    against Alachua (CO_NO=11, first county, physically early in the
    table -> instant, correct results) vs. Pasco (CO_NO=61, physically
    ~50th of 67 -> same timeout as the attribute filter). This is a
    real backend limitation of this specific hosted mirror, not a fixable
    query-syntax issue.
  - DECISION: county-scoped work now uses each county's OWN parcel/
    cadastral layer (see parcel_service_url etc. below), confirmed live
    per-county. STATEWIDE_CADASTRAL_URL is kept only as a fallback for
    cross-county work where a DOR_UC-only filter (no CO_NO) is fast
    enough -- e.g. a first-pass filter before a candidate's county is
    known.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CountyEndpoint:
    id: str
    name: str
    fips: int  # Florida DOR county number. NOTE: not usable as a WHERE-clause
    # filter against STATEWIDE_CADASTRAL_URL -- see the CO_NO ground-truth
    # note above. Still correct as an identifier/for display.
    flum_service_url: str
    flu_field: str
    jurisdiction_field: Optional[str]
    acreage_field: Optional[str]
    notes: str
    usb_layer_url: Optional[str] = None
    agricultural_flu_values: tuple = field(default_factory=tuple)

    # -- Per-county PARCEL/cadastral layer (separate from the FLUM layer
    # above) -- confirmed live via describe_layer (?f=pjson) the same way
    # as the FLUM layer, per-county, 2026-07-03. This is what acreage/
    # ag-use-code/owner filtering should run against now, instead of the
    # statewide cadastral layer's broken CO_NO filter.
    parcel_service_url: Optional[str] = None
    parcel_use_code_field: Optional[str] = None
    # Explicit confirmed use-code VALUES, not a range. Two counties tested
    # here (St. Johns, Osceola) use a 4-character local use-code scheme
    # (not the statewide 3-char DOR_UC), and a naive string range copy-
    # pasted from DOR_UC ('050','069') silently matches unrelated codes
    # under lexicographic string comparison (e.g. Osceola's '0611'
    # "RETIREMENT HOMES" falls between '050' and '069' as a STRING even
    # though it is not an agricultural code) -- confirmed by hitting this
    # exact bug live against Osceola's DORCode field. Use an explicit
    # value list/IN-clause instead of a range unless a range has been
    # individually verified not to catch false positives.
    parcel_agricultural_use_codes: tuple = field(default_factory=tuple)
    # Separate from the field above: a (min, max) STRING range, only for
    # counties where a range comparison on parcel_use_code_field was
    # actually spot-checked against real data and didn't show the
    # lexicographic false-positive problem. Prefer parcel_agricultural_use_codes
    # (explicit list) unless a range has been checked this way.
    parcel_agricultural_use_code_range: Optional[tuple] = None
    parcel_acreage_field: Optional[str] = None
    parcel_owner_field: Optional[str] = None
    parcel_owner_field_2: Optional[str] = None  # secondary owner/co-owner, where present
    parcel_id_field: Optional[str] = None
    # Extra WHERE-clause fragment needed to scope a shared multi-county
    # layer down to just this county (e.g. Nassau's parcel layer also
    # contains Baker County). None if the layer is already single-county.
    parcel_county_filter: Optional[str] = None
    # Jurisdiction field on the PARCEL layer specifically -- separate from
    # `jurisdiction_field` above, which is the FLUM layer's field name and
    # is NOT guaranteed to match (e.g. Osceola's FLUM layer uses
    # "Jurisdiction" but its parcel layer uses "Jurisdicti" -- confirmed
    # live: passing the FLUM name in the parcel layer's outFields returns
    # an ArcGIS 400 "Unable to complete operation" error). None if the
    # parcel layer has no jurisdiction field at all.
    parcel_jurisdiction_field: Optional[str] = None


COUNTIES: dict[str, CountyEndpoint] = {

    "hillsborough": CountyEndpoint(
        id="hillsborough",
        name="Hillsborough",
        fips=39,  # CORRECTED from 57
        flum_service_url=(
            "https://maps.hillsboroughcounty.org/arcgis/rest/services/"
            "DSD_Viewer_Services/DSD_Viewer_Planning/MapServer/1"
        ),
        flu_field="FLUE",
        jurisdiction_field="JURISDICTI",
        acreage_field="ACREAGE",
        agricultural_flu_values=("A", "A/M", "A/R", "AE"),
        usb_layer_url=None,
        notes=(
            "Confirmed live FeatureLayer with advanced queries, full polygon "
            "geometry, FLU code + description + acreage + jurisdiction. "
            "Covers unincorporated county plus Plant City and Temple Terrace."
        ),
    ),

    "orange": CountyEndpoint(
        id="orange",
        name="Orange",
        fips=58,  # CORRECTED from 48
        flum_service_url=(
            "https://ocgis4.ocfl.net/arcgis/rest/services/"
            "AGOL_Open_Data/MapServer/21"
        ),
        flu_field="FLU",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("Rural/Agricultural",),
        notes=(
            "Confirmed live FeatureLayer. Exact field names should be "
            "re-confirmed via /query?f=pjson before production use."
        ),
    ),

    "pasco": CountyEndpoint(
        id="pasco",
        name="Pasco",
        fips=61,  # CORRECTED from 51
        flum_service_url=(
            "https://mapping.pascopa.com/arcgis/rest/services/"
            "Land_Use/MapServer/0"
        ),
        # CONFIRMED live 2026-07-03 via describe_layer (?f=pjson) + a full
        # distinct-values query (48 combinations, 1476 features) while
        # running the full scan pipeline end-to-end for the first time.
        # The prior COMP_LAND_/"Agricultural/Rural" pairing was a guess
        # that does not exist on this layer at all -- the real field is
        # FLU_CODE, and the real agricultural codes are 'AG'
        # ("AGRICULTURAL-.1 du/ga*") and 'AG/R'
        # ("AGRICULTURAL/RURAL-.2 du/ga*"). This bug meant every
        # encirclement check for Pasco was silently comparing against a
        # field that always returned None, before it was ever run live.
        flu_field="FLU_CODE",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AG", "AG/R"),
        notes=(
            "FLUM layer (Land_Use/MapServer/0): CONFIRMED via live "
            "describe_layer + full distinct-values query 2026-07-03 "
            "(see flu_field/agricultural_flu_values above -- this "
            "replaces the earlier unconfirmed COMP_LAND_ guess). "
            "PARCEL layer (Parcels/MapServer/3, 'Parcels (Clickable "
            "Info)'): CONFIRMED via live describe_layer 2026-07-03 and "
            "test query. Real field names: DIR_CLASS (3-char DOR-style "
            "use code, confirmed values '054' Timberland-adjacent, '068' "
            "seen on a real parcel), VAL_ACRES and TR_AC (acreage, "
            "identical in the one sample checked), NAD_NAME_1/NAD_NAME_2 "
            "(owner/co-owner), ParcelID, PHYS_STREET/PHYS_CITY/PHYS_STATE/"
            "PHYS_ZIP (situs address). No jurisdiction field on this "
            "layer -- unincorporated-status filtering not available here "
            "(known gap, see main scanner notes)."
        ),
        parcel_service_url=(
            "https://mapping.pascopa.com/arcgis/rest/services/"
            "Parcels/MapServer/3"
        ),
        parcel_use_code_field="DIR_CLASS",
        # Confirmed via live query: `DIR_CLASS>='050' AND DIR_CLASS<='069'`
        # returned real ag parcels (owner "FOREST PROPERTIES LLC", 18-72
        # acres, codes '054' and '068'). Unlike St. Johns/Osceola, Pasco's
        # DIR_CLASS is a 3-char field and the string range did NOT show
        # the lexicographic false-positive problem in the sample checked
        # -- but only ~5 rows were inspected, so treat this range as
        # provisionally confirmed, not exhaustively verified.
        parcel_agricultural_use_code_range=("050", "069"),
        parcel_acreage_field="VAL_ACRES",
        parcel_owner_field="NAD_NAME_1",
        parcel_owner_field_2="NAD_NAME_2",
        parcel_id_field="ParcelID",
    ),

    "sarasota": CountyEndpoint(
        id="sarasota",
        name="Sarasota",
        fips=68,  # CORRECTED from 58
        flum_service_url=(
            "https://data-sarco.opendata.arcgis.com/documents/"
            "sarco::future-land-use/about"
        ),
        flu_field="LU_DESC",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("Rural", "Semi-Rural"),
        usb_layer_url=None,
        notes=(
            "Substantially resolved but field name and exact FeatureServer "
            "URL still need one describe_layer() confirmation call."
        ),
    ),

    "manatee": CountyEndpoint(
        id="manatee",
        name="Manatee",
        fips=51,  # CORRECTED from 41
        flum_service_url=(
            "https://public-manateegis.opendata.arcgis.com/"
            "maps/manateegis::future-land-use"
        ),
        flu_field="FLUNAME",
        jurisdiction_field="CITY_NAME",
        acreage_field="Acres",
        agricultural_flu_values=("Agriculture/Rural", "AG", "A"),
        notes=(
            "Substantially resolved; alternate host www.mymanatee.org/"
            "gisits/rest/services/opendata/Planning/FeatureServer available "
            "if the dedicated FLU service changes."
        ),
    ),

    "brevard": CountyEndpoint(
        id="brevard",
        name="Brevard",
        fips=15,  # CORRECTED from 5
        flum_service_url=(
            "https://gis.brevardfl.gov/gissrv/rest/services/"
            "Planning_Development/FLU_WKID2881/MapServer/0"
        ),
        flu_field="FLU",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AGRIC",),
        notes=(
            "Fully resolved. Native spatial reference is WKID 2881 "
            "(Florida State Plane East, feet) -- reproject before combining "
            "with web-mercator parcel geometry. Data may not be resold "
            "without Brevard BOCC consent -- fine for internal screening."
        ),
    ),

    "volusia": CountyEndpoint(
        id="volusia",
        name="Volusia",
        fips=74,  # CORRECTED from 64
        flum_service_url=(
            "https://maps1.vcgov.org/arcgis/rest/services/"
            "Land_Use_Zoning/MapServer/1"
        ),
        flu_field="LUNAME",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("RURAL",),
        notes=(
            "Fully resolved. Native spatial reference is WKID 2881, same "
            "as Brevard -- reproject before combining with web-mercator "
            "parcel layers."
        ),
    ),

    # Added for Tyler's current target counties -- flu_field values below
    # are UNCONFIRMED (marked so deliberately). Statewide cadastral
    # DOR_UC filtering will work now that fips is correct; FLUM-specific
    # scanning needs each of these confirmed via one describe_layer() call
    # before relying on flu_field/agricultural_flu_values.
    "st_johns": CountyEndpoint(
        id="st_johns",
        name="St. Johns",
        fips=65,
        # CONFIRMED live 2026-07-03 via describe_layer (?f=pjson) + test
        # query. Host found via web search (gis.sjcfl.us was the guessed
        # bare hostname and does not resolve -- the real host is
        # www.gis.sjcfl.us, under a /portal_sjcgis/ path, not /arcgis/).
        flum_service_url=(
            "https://www.gis.sjcfl.us/portal_sjcgis/rest/services/"
            "Future_Land_Use/MapServer/0"
        ),
        flu_field="FUTLUSE1",
        # No jurisdiction field on this layer. Oddly, incorporated cities
        # appear AS distinct FUTLUSE1 categories themselves (e.g. 'CITY OF
        # ST. AUGUSTINE', 'CITY OF ST. AUGUSTINE BEACH', 'TOWN OF
        # MARINELAND') rather than via a separate jurisdiction flag --
        # confirmed via a full distinct-values query (28 categories
        # total). Practical effect: excluding those exact FUTLUSE1 string
        # values IS the unincorporated filter for this county, just
        # folded into the same field instead of a separate one.
        jurisdiction_field=None,
        acreage_field=None,
        # Confirmed via live distinct-values query (28 total categories).
        # 'RUR/SYLV' and 'RUR/SYLV/SJRWMD' (rural/silviculture) also exist
        # and may be enclave-relevant but are NOT confirmed as
        # "agricultural" for statute purposes -- flagged separately, not
        # included below until reviewed.
        agricultural_flu_values=("AGRICULTURE",),
        notes=(
            "FLUM layer CONFIRMED live + describe_layer-tested. Values "
            "CITY OF ST. AUGUSTINE / CITY OF ST. AUGUSTINE BEACH / TOWN "
            "OF MARINELAND / JULINGTON CREEK DRI / CABALLOS DEL MAR DRI / "
            "ST. JOHNS DRI appear as FUTLUSE1 categories -- treat these "
            "as incorporated/DRI exclusions when filtering. RUR/SYLV and "
            "RUR/SYLV/SJRWMD categories exist but are unreviewed for ag "
            "relevance. PARCEL layer (Parcel/MapServer/0): CONFIRMED live "
            "+ test-queried. USE_CODE is a 4-char COUNTY-LOCAL code, NOT "
            "the statewide DOR_UC (confirmed real samples: '0100' Single "
            "Family, '9900' Acreage Not Zoned Agricultural -- note this "
            "specific code has 'Acreage' in its name but is explicitly "
            "NOT agricultural, a real trap). No populated acreage field "
            "on the parcel layer -- Shape_STArea__ returned 0.0 on every "
            "sampled row; acreage must be computed from polygon geometry "
            "(Web Mercator SR, wkid 3857) once fetched, not read directly."
        ),
        parcel_service_url=(
            "https://www.gis.sjcfl.us/portal_sjcgis/rest/services/"
            "Parcel/MapServer/0"
        ),
        parcel_use_code_field="USE_CODE",
        # Confirmed via live LIKE-based search across USE_DESC (200-row
        # sample): these are the real distinct agricultural codes found.
        # Deliberately EXCLUDES '9900' ("Acreage Not Zoned Agricultural")
        # even though it sounds agricultural -- confirmed not to be.
        parcel_agricultural_use_codes=("5300", "5500", "5900", "6200", "6900"),
        parcel_acreage_field=None,  # not populated -- compute from geometry
        parcel_owner_field="PRP_NAME",
        parcel_id_field="PIN",
    ),
    "nassau": CountyEndpoint(
        id="nassau",
        name="Nassau",
        fips=55,
        # CONFIRMED live 2026-07-03. Found via arcgis.com item search, not
        # the county's own bare hostname (no working bare gis.* host was
        # found for Nassau) -- this is a hosted FeatureServer owned by
        # kmulcahy@nassauflpa.com (Nassau County Property Appraiser staff
        # account), so treated as authoritative.
        flum_service_url=(
            "https://services5.arcgis.com/F73IhFZbCCYUexxB/arcgis/rest/"
            "services/Unincorporated_Nassau_County_Future_Land_Use_/"
            "FeatureServer/156"
        ),
        flu_field="FLUM",
        # No separate jurisdiction field NEEDED: this entire layer is
        # pre-filtered to unincorporated land at the source (it's titled
        # "Unincorporated Nassau County Future Land Use" and is served
        # from the Property Appraiser's own account) -- confirmed this is
        # the layer's actual, intentional scope, not an assumption. This
        # effectively satisfies the unincorporated-status filter for
        # Nassau for free, unlike the other three target counties.
        jurisdiction_field=None,
        acreage_field="Acre",
        # Confirmed via live distinct-values query (23 categories total).
        agricultural_flu_values=("Agriculture",),
        notes=(
            "FLUM layer CONFIRMED live + describe_layer-tested; layer ID "
            "is 156, not 0 -- the FeatureServer root must be checked for "
            "the real layer id, it is not always 0. Pre-filtered to "
            "unincorporated land at the source (see jurisdiction_field "
            "note). Has a direct Acre field (unlike St. Johns/Osceola's "
            "FLUM layers). PARCEL layer: 'Parcels in Baker and Nassau "
            "Counties' -- a REGIONAL layer covering two counties, not "
            "Nassau-only; must filter CNTYNAME='NASSAU' (confirmed "
            "UPPERCASE via live query) to scope it. CONFIRMED via live "
            "query: DORUC field IS the real 3-char statewide DOR use "
            "code (values '050','055','056' seen on real parcels -- "
            "Rayonier Forest Resources LP timberland, 17-646 acres), a "
            "clean match with no lexicographic false-positive issue "
            "found. Direct ACRES field populated and correct-looking in "
            "the sample checked."
        ),
        parcel_service_url=(
            "https://services2.arcgis.com/PYn6bWCjT6bhw1z3/arcgis/rest/"
            "services/Parcels_in_Baker_and_Nassau_Counties/FeatureServer/0"
        ),
        parcel_county_filter="CNTYNAME='NASSAU'",
        parcel_use_code_field="DORUC",
        parcel_agricultural_use_code_range=("050", "069"),
        parcel_acreage_field="ACRES",
        parcel_owner_field="ONAME",
        parcel_id_field="PARCELID",
    ),
    "osceola": CountyEndpoint(
        id="osceola",
        name="Osceola",
        fips=59,
        # CONFIRMED live 2026-07-03. County's own ArcGIS Enterprise portal
        # -- gis.osceola.org/hosting/rest/services (NOT /arcgis/rest/
        # services, which 404s on this host).
        flum_service_url=(
            "https://gis.osceola.org/hosting/rest/services/"
            "Future_Land_Use/FeatureServer/12"
        ),
        flu_field="FLU",
        # CONFIRMED real, usable field with clean values: 'Unincorporated'
        # vs. 'incorporated' (with the specific city as a second field --
        # 'Kissimmee', 'St. Cloud', 'R.C.I.D.'). This is exactly the
        # unincorporated hard filter the scanner is currently missing --
        # confirmed via a full distinct-values query (21 combinations).
        jurisdiction_field="Jurisdiction",
        acreage_field="AC",
        # Confirmed via live distinct-values query. Lowercase in the real
        # data ('rural/agricultural'), not title case -- filter must
        # match exact case or use UPPER()/case-insensitive comparison.
        agricultural_flu_values=("rural/agricultural",),
        notes=(
            "FLUM layer CONFIRMED live + describe_layer-tested; layer id "
            "is 12, not 0. Jurisdiction field is clean and directly "
            "usable ('Unincorporated' / 'incorporated' + city name) -- "
            "the best-supported unincorporated filter found across all "
            "four target counties. 'rural enclave' and 'rural settlement' "
            "categories also exist and may be enclave-relevant but are "
            "NOT included in agricultural_flu_values pending review. "
            "PARCEL layer (Parcels/FeatureServer/3): CONFIRMED live + "
            "test-queried. IMPORTANT BUG CAUGHT LIVE: this layer's "
            "'DORCode' field is named like the statewide DOR_UC but is "
            "actually a 4-char COUNTY-LOCAL code -- a naive string range "
            "copy-pasted from DOR_UC ('050','069') silently matched "
            "'0611' (RETIREMENT HOMES) because '0611' sorts lexically "
            "between '050' and '069' as a STRING despite being an "
            "unrelated code. Real confirmed agricultural codes (verified "
            "via DORDesc text search, then cross-checked that "
            "CAST(DORCode AS INTEGER) BETWEEN 5000 AND 6999 returns ONLY "
            "genuinely agricultural descriptions with no false positives, "
            "14 distinct codes total) are the explicit list below -- use "
            "an integer cast + range, or the explicit list, NEVER a plain "
            "string range on this field. Has a direct, populated "
            "TotalAcres field and a parcel-level Jurisdicti/JurisDesc "
            "pair mirroring the FLUM layer's jurisdiction field."
        ),
        parcel_service_url=(
            "https://gis.osceola.org/hosting/rest/services/"
            "Parcels/FeatureServer/3"
        ),
        parcel_use_code_field="DORCode",
        parcel_agricultural_use_codes=(
            "5101", "5111", "5501", "5601", "5701", "5711",
            "6001", "6011", "6046", "6601", "6611", "6711", "6901", "6911",
        ),
        parcel_acreage_field="TotalAcres",
        parcel_owner_field="Owner1",
        parcel_owner_field_2="Owner2",
        parcel_id_field="PARCELNO",
        # jurisdiction_field above (set to "Jurisdiction") is the FLUM
        # layer's field; the parcel layer carries the same concept under
        # a different name -- Jurisdicti (code, used here) / JurisDesc
        # (plain text). Confirmed live: using the FLUM field name
        # ("Jurisdiction") in the parcel layer's outFields fails with an
        # ArcGIS 400 error, since that field doesn't exist on this layer.
        parcel_jurisdiction_field="Jurisdicti",
    ),
}


# Statewide parcel/cadastral layer -- single source of truth for ownership,
# acreage, DOR land use code, and sale history across all 67 counties.
# Confirmed live with advanced queries; filter by CO_NO (county DOR number).
STATEWIDE_CADASTRAL_URL = (
    "https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/"
    "Florida_Statewide_Cadastral/FeatureServer/0"
)

# DOR land use codes in the agricultural range.
#
# CORRECTED 2026-07-03 back to the FDOR-documented range: the statewide
# DOR_UC field (confirmed against FDOR's own published NAL Users Guide) is
# a 3-character code from '000' to '099'. Agricultural classifications
# (cropland, timberland, pasture, orchard/grove, poultry/dairy, etc.) run
# '050' through '069'. The previous (5000, 6999) range in this file was
# very likely confusing this field with PA_UC -- a county-*specific*,
# locally-defined use code (e.g. Lee County's own 4-digit scheme) that is
# a completely different field and does not apply across counties. Filtering
# DOR_UC against a 4-digit range would never match anything, since DOR_UC
# never exceeds 099 -- this was almost certainly why scans returned empty.
DOR_AGRICULTURAL_UC_RANGE = ("050", "069")
