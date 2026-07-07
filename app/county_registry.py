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

    # -- Population + live-confirmation status (added for the wizard UI
    # rebuild, 2026-07-06). population is an approximate 2024 Census
    # Bureau county population estimate (public data, not live-queried
    # from any GIS layer) -- used only for the statutory <=1.75M county
    # population cap display/check, s. 163.3164(4), F.S. confirmed_live
    # reflects whether THIS county's FLUM/parcel field names have
    # actually been verified via a live describe_layer() call (see each
    # county's own `notes` above) -- Orange, Sarasota, and Manatee are
    # explicitly NOT set True below despite being reachable endpoints,
    # since their exact field names are still unconfirmed guesses per
    # their own notes. Deliberately separate from the old ad hoc
    # flu_field heuristic in main.py's list_counties(), which conflated
    # "reachable" with "field names confirmed."
    population: int = 0
    confirmed_live: bool = False

    # -- Per-county PARCEL/cadastral layer (separate from the FLUM layer
    # above) -- confirmed live via describe_layer (?f=pjson) the same way
    # as the FLUM layer, per-county, 2026-07-03. This is what acreage/
    # ag-use-code/owner filtering should run against now, instead of the
    # statewide cadastral layer's broken CO_NO filter.
    parcel_service_url: Optional[str] = None
    # Identifier for the underlying data source, used by service_windows.py
    # to enforce availability constraints. None means "24/7 direct-county
    # source, no window" (Pasco's mapping.pascopa.com, Nassau's/St.
    # Johns'/Osceola's own layers, Duval's coj.net, Lee/Leon/Citrus's
    # respective county services). Set to "swfwmd_parcel_search" for
    # counties fetched via SWFWMD's shared parcel_search MapServer
    # (docs: 6 AM - 10 PM Eastern availability only). CONCENTRATION-RISK
    # NOTE: every county sharing the same non-None parcel_source depends
    # on that single upstream mirror -- if it changes schema or goes
    # down, every county through it is affected simultaneously, unlike
    # the direct-county sources each of which has its own failure domain.
    parcel_source: Optional[str] = None
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

    # -- Post-1/1/2025 ownership-change flag (statutory gap #3, research
    # pass 2026-07-04) -- a real sale-date field exists on every county's
    # parcel layer but the encoding differs; statutory_checks.py decodes
    # per this value. One of:
    #   "ymd_ints"     -- separate year/month/day integer fields (Pasco)
    #   "year_only"    -- a single year integer field only (Nassau) --
    #                     sufficient for this cutoff since sale year >=
    #                     2025 is equivalent to "on or after 1/1/2025"
    #                     with no month/day precision needed
    #   "excel_serial" -- integer day count since 1899-12-30 (St. Johns;
    #                     encoding INFERRED from value magnitude, not
    #                     confirmed by documentation -- see STATUS.md)
    #   "epoch_millis" -- standard Esri date field, ms since Unix epoch
    #                     (Osceola)
    sale_date_encoding: Optional[str] = None
    sale_year_field: Optional[str] = None
    sale_month_field: Optional[str] = None
    sale_day_field: Optional[str] = None
    sale_date_field: Optional[str] = None  # single combined field, for excel_serial/epoch_millis

    # -- Unincorporated-status hard filter (statutory gap #1, research
    # pass 2026-07-04). One of:
    #   "flum_jurisdiction_join"          -- parcel layer's own jurisdiction
    #                                        field is NULL on every row;
    #                                        spatially join the candidate
    #                                        against the FLUM layer and read
    #                                        jurisdiction_field off whichever
    #                                        FLUM polygon(s) intersect it
    #                                        (Osceola)
    #   "already_filtered"                -- the layer is pre-scoped to
    #                                        unincorporated land at the
    #                                        source, no query needed (Nassau)
    #   "flum_incorporated_flu_exclude"   -- no jurisdiction field anywhere;
    #                                        incorporated cities appear as
    #                                        their own FLU category strings
    #                                        on the FLUM layer
    #                                        (incorporated_flu_values below)
    #                                        -- spatially join and exclude
    #                                        those exact values (St. Johns)
    #   "manual_only"                     -- no automated check available.
    #                                        A live point-in-polygon query
    #                                        DISPROVED the "layer is
    #                                        unincorporated-only by
    #                                        home-rule construction"
    #                                        assumption for Pasco (confirmed
    #                                        2026-07-05: Port Richey City
    #                                        Hall intersects a real 266.7-
    #                                        acre FLUM feature) -- do not
    #                                        wire an automated pass/fail
    #                                        here, see STATUS.md
    #   "city_limits_layer_join"          -- spatial join against a real,
    #                                        dedicated city-limits
    #                                        FeatureServer (NOT the FLUM
    #                                        layer) -- see
    #                                        city_limits_layer_url/
    #                                        city_limits_field below. Added
    #                                        2026-07-06 for Pasco once a
    #                                        real Pasco_BOCC-owned City_
    #                                        Limits layer was found,
    #                                        replacing manual_only.
    unincorporated_check: str = "manual_only"
    incorporated_flu_values: tuple = field(default_factory=tuple)
    city_limits_layer_url: Optional[str] = None
    city_limits_field: Optional[str] = None

    # -- Urban Service Area (USB) approximation, added 2026-07-06 for
    # Pasco only. Pasco's own comprehensive plan (Map 2-22, "Urban
    # Service Area / Rural Area / Expansion Area") draws a single binary
    # boundary between Rural Area and Urban Service Area -- there's no
    # separate USB-specific layer, but a real, Pasco_BOCC-adjacent
    # "Rural Areas Current" FeatureServer exists (owner
    # djohnson_pascocounty, real ordinance references like "ORD 25-15"
    # in its `gensis` field). A parcel NOT entirely within a Rural Area
    # polygon is treated as touching the Urban Service Area -- this is
    # an approximation (it doesn't distinguish "just outside the Rural
    # Area line" from "deep in the middle of a city"), not a from-the-
    # source USB layer, so it's kept as its own distinct field rather
    # than silently reusing usb_layer_url above (which stays reserved for
    # a real, direct USB layer if one is ever found for any county).
    rural_area_layer_url: Optional[str] = None

    # -- FLWMI (Florida Water Management Inventory, FDOH) join key
    # transform, added alongside flwmi_client.py 2026-07-06. Confirmed
    # live: FLWMI's PARCELNO matches Pasco's ParcelID and Osceola's
    # PARCELNO byte-for-byte with no transform. St. Johns is the one
    # confirmed exception -- FLWMI's PARCELNO is a bare 10-digit string
    # (e.g. "0000200010") but this county's own PIN field is
    # space-separated (e.g. "010832 0010") -- "strip_spaces" tells
    # flwmi_client to remove spaces from the local PIN before joining.
    # None means "join with the parcel id field as-is, no transform
    # needed" (confirmed for Pasco/Osceola; assumed-but-not-yet-cross-
    # checked-against-a-real-sample for Nassau, same dash pattern).
    flwmi_parcel_id_transform: Optional[str] = None


# Statutory maximum county population for agricultural-enclave eligibility
# under s. 163.3164(4)(f), F.S. ("Are located within a county with a
# population of 1.75 million or less."). Enforced in main.scan_county;
# every current registry entry is well under this, so the check is a
# defensive guard rather than an active filter today -- but it needs to
# be an explicit, visible check so a future Miami-Dade (~2.6M) or Broward
# (~1.9M) addition doesn't silently pass.
POPULATION_CAP = 1_750_000


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
        population=1584000,
        confirmed_live=True,
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
        population=1466000,
        confirmed_live=False,  # field names unconfirmed, see notes below
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
        # BEBR April 1, 2024 estimate. Well under s. 163.3164(4)(f)'s
        # 1.75M population cap (~36% of the cap).
        population=633029,
        confirmed_live=True,
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
        # Confirmed via live sample: SALE_YEAR/SALE_MON/SALE_DAY are
        # separate full-precision ints (e.g. 2018-05-02, $30,000 SALE_AMT).
        sale_date_encoding="ymd_ints",
        sale_year_field="SALE_YEAR",
        sale_month_field="SALE_MON",
        sale_day_field="SALE_DAY",
        # RESOLVED 2026-07-06: found a real, dedicated Pasco_BOCC-owned
        # City_Limits FeatureServer (via ArcGIS Online item search),
        # created 2025-01-23, CITYNAME field with real per-city polygons
        # (New Port Richey, Port Richey, San Antonio, St Leo, Dade City,
        # etc). Confirmed live: a point built from the centroid of one of
        # this layer's own real Port Richey polygon fragments correctly
        # returns CITYNAME='Port Richey'; the same known-unincorporated
        # control point used in the prior FLUM-based test (28.41, -82.66)
        # correctly returns no hit. This is a different, independent
        # dataset from the FLUM layer that disproved the earlier
        # home-rule inference -- no longer manual_only.
        unincorporated_check="city_limits_layer_join",
        city_limits_layer_url=(
            "https://services6.arcgis.com/Mo4MddfRHpFwT7UF/arcgis/rest/"
            "services/City_Limits/FeatureServer/0"
        ),
        city_limits_field="CITYNAME",
        # Confirmed live 2026-07-06: a point known to sit inside a real
        # Rural Area polygon ("AREA 3") correctly returns a
        # esriSpatialRelWithin hit; a point in downtown New Port Richey
        # (clearly urban/incorporated) correctly returns no hit.
        rural_area_layer_url=(
            "https://services6.arcgis.com/Mo4MddfRHpFwT7UF/arcgis/rest/"
            "services/RuralAreas_Current/FeatureServer/0"
        ),
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
        population=448000,
        confirmed_live=False,  # field name + exact URL unconfirmed, see notes
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
        population=430000,
        confirmed_live=False,  # field name unconfirmed, see notes below
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
        population=647000,
        confirmed_live=True,
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
        population=583000,
        confirmed_live=True,
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
        # BEBR April 1, 2024 estimate. Well under s. 163.3164(4)(f)'s
        # 1.75M population cap (~19% of the cap).
        population=331479,
        confirmed_live=True,
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
        # SALEDATE values like 38520/37741 -- near-certainly Excel/OLE
        # serial day count (days since 1899-12-30), NOT YYYYMMDD (wrong
        # magnitude for that). Encoding INFERRED from value magnitude, not
        # confirmed by documentation -- flagged as such in STATUS.md.
        sale_date_encoding="excel_serial",
        sale_date_field="SALEDATE",
        # No jurisdiction field anywhere on this county's layers (confirmed
        # via field grep) -- incorporated cities appear as their own
        # FUTLUSE1 categories instead (28-category live distinct-values
        # query). These are the exact incorporated-city values found.
        unincorporated_check="flum_incorporated_flu_exclude",
        incorporated_flu_values=(
            "CITY OF ST. AUGUSTINE",
            "CITY OF ST. AUGUSTINE BEACH",
            "TOWN OF MARINELAND",
        ),
        # Confirmed live 2026-07-06: FLWMI's PARCELNO for this county is a
        # bare 10-digit string ("0000200010") with no spaces, but this
        # county's own PIN field is space-separated ("010832 0010") --
        # strip spaces from PIN before joining to FLWMI.
        flwmi_parcel_id_transform="strip_spaces",
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
        # BEBR April 1, 2024 estimate. Well under s. 163.3164(4)(f)'s
        # 1.75M population cap (~6% of the cap).
        population=103990,
        confirmed_live=True,
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
        # SALEYR1 is year-only (no month/day) -- confirmed sufficient for
        # this specific cutoff, since the statute's exact 1/1/2025 date
        # means "sale year >= 2025" has no precision loss vs. a full date.
        sale_date_encoding="year_only",
        sale_year_field="SALEYR1",
        # FLUM layer is pre-scoped to unincorporated land at the source
        # (see jurisdiction_field note above) -- no spatial join needed.
        unincorporated_check="already_filtered",
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
        # BEBR April 1, 2024 estimate. Well under s. 163.3164(4)(f)'s
        # 1.75M population cap (~26% of the cap).
        population=451231,
        confirmed_live=True,
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
        # SaleDate/PrevSaleDa are standard Esri epoch-millis date fields --
        # cleanest encoding of the four counties (confirmed live sample:
        # 1690848000000 = 2023-08-01).
        sale_date_encoding="epoch_millis",
        sale_date_field="SaleDate",
        # Parcel layer's own Jurisdicti/JurisDesc are NULL on every sampled
        # row (confirmed 2026-07-03) -- the FLUM layer's Jurisdiction field
        # is populated and must be reached via a spatial join instead.
        unincorporated_check="flum_jurisdiction_join",
    ),
    # =====================================================================
    # Scale-Up Phase 3 -- Wave 1 additions, 2026-07-06.
    # Same rigor as the four pilot counties -- describe_layer live, real
    # field names and ag classification values confirmed via live sample
    # queries (see per-county notes below). BUT: FLUM agricultural values
    # are Wave-1 best-guess from a 500-row distinct-values sample, NOT
    # exhaustively verified against every FLUM category. Refine in a
    # follow-up if a real scan surfaces obvious misclassification.
    # =====================================================================
    "lee": CountyEndpoint(
        id="lee",
        name="Lee",
        fips=71,  # FL DOR county number for Lee
        # FLUM: Lee County Planning Tool layer 0, real field 'Main_FLU'.
        # ag_flu_values REFINED 2026-07-06 via a full paginated distinct-
        # values pull (90+ distinct FLU codes) + a 3-parcel FLUM-at-
        # centroid test using real ag parcels from Wave 1's scan. Lee's
        # FLUM has "Rural" (65x), "Coastal Rural" (8x), "Rural Community
        # Preserve" (3x), and Lee-specific "Density Reduction/Groundwater
        # Resource" (726x + 18x abbrev "DRGR") as its low-density non-
        # residential categories -- all included so a parcel bordered by
        # these doesn't get miscounted as "adjacent to residential" for
        # encirclement math. Wave 1's initial best-guess of just
        # ("Coastal Rural",) was too narrow.
        flum_service_url=(
            "https://services8.arcgis.com/7tOcoRLUBt73R0wV/arcgis/rest/"
            "services/Lee_County_Planning_Tool_WFL1/FeatureServer/0"
        ),
        flu_field="Main_FLU",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=(
            "Rural",
            "Coastal Rural",
            "Rural Community Preserve",
            "Density Reduction/Groundwater Resource",
            "DRGR",
            "Lee County FLUM: DRGR",
            "Open Lands",
        ),
        population=822571,  # BEBR 2024 estimate. Under 1.75M cap.
        confirmed_live=True,
        notes=(
            "Wave 1 (2026-07-06), refined during Wave 1 validation "
            "pass. PARCEL layer: DORCODE (2-char, '50'-'69' range "
            "confirmed ag with LANDUSEDES='MARKET VALUE AGRICULTURAL'), "
            "GISACRES (Double, acreage in acres, no conversion needed), "
            "O_NAME (single owner name, no co-owner), STRAP (parcel ID). "
            "FLUM Main_FLU: real ag/rural values confirmed via FLUM-at-"
            "parcel-centroid test on 3 known ag parcels. Wave-1 initial "
            "guess (just 'Coastal Rural') REFINED to include Rural + "
            "DRGR + Open Lands + Rural Community Preserve. "
            "Unincorporated check WIRED via Lee's Planning Tool layer "
            "4 (Municipal Boundaries) with CityName field -- real "
            "values include 'Town of Fort Myers Beach', 'City of Sanibel', "
            "'City of Cape Coral', etc."
        ),
        parcel_service_url=(
            "https://services2.arcgis.com/LvWGAAhHwbCJ2GMP/arcgis/rest/"
            "services/Lee_County_Parcels/FeatureServer/0"
        ),
        parcel_use_code_field="DORCODE",
        parcel_agricultural_use_code_range=("50", "69"),
        parcel_acreage_field="GISACRES",
        parcel_owner_field="O_NAME",
        parcel_owner_field_2=None,
        parcel_id_field="STRAP",
        # Unincorporated check: use Lee's Municipal Boundaries layer 4
        # under the Planning Tool bundle. Wave-1 validation added.
        unincorporated_check="city_limits_layer_join",
        city_limits_layer_url=(
            "https://services8.arcgis.com/7tOcoRLUBt73R0wV/arcgis/rest/"
            "services/Lee_County_Planning_Tool_WFL1/FeatureServer/4"
        ),
        city_limits_field="CityName",
    ),
    "citrus": CountyEndpoint(
        id="citrus",
        name="Citrus",
        fips=8,
        # FLUM: Citrus_County_Data_WFL1 layer 7 (LandUse). Real field
        # 'LANDUSE'. ag_flu_values REFINED 2026-07-06: full paginated
        # distinct pull shows RMU (18838x), RUR (1379x), CL (1099x),
        # MDR (291x), GNC (257x), IND (61x), AGR (40x), TCU (13x), LDR
        # (8x), CRR (7x), PSO (4x), CLC (2x), PSI (1x). FLUM-at-parcel-
        # centroid test on 3 known ag parcels showed ag parcels
        # actually sit in AGR + RUR (not just RUR as Wave 1 guessed).
        # Adding AGR was mandatory -- Wave-1 guess missed the primary
        # ag designation.
        flum_service_url=(
            "https://services1.arcgis.com/q8sarOko6mCDwiGm/arcgis/rest/"
            "services/Citrus_County_Data_WFL1/FeatureServer/7"
        ),
        flu_field="LANDUSE",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AGR", "RUR"),
        population=170453,
        confirmed_live=True,
        notes=(
            "Wave 1 (2026-07-06), refined during Wave 1 validation. "
            "PARCEL layer: LUC (4-char, CAST-to-int + blank-string "
            "guard; real values '5000'-'6900'), OWN1/OWN2, ALTKEY "
            "(parcel ID). No confirmed acres field -- SQFT exists but "
            "isn't the right unit; parcel_acreage_field=None so "
            "_extract_acreage falls back to geometry-based computation "
            "(same as St. Johns). CITYNAME field is postal (e.g. "
            "'CRYSTAL RIVER') not jurisdictional. FLUM ag_flu_values "
            "'AGR' + 'RUR' confirmed via FLUM-at-parcel-centroid test "
            "on 3 known ag parcels (Wave-1 guess of just 'RUR' missed "
            "the primary AGR designation). Unincorporated check WIRED "
            "via Citrus's own CityBoundaries layer 9 with CORPNAME "
            "field -- real values 'CRYSTAL RIVER', 'INVERNESS'."
        ),
        parcel_service_url=(
            "https://services1.arcgis.com/5hzvezV1fsP5byjX/arcgis/rest/"
            "services/Citrus_County__FL_Parcels/FeatureServer/11"
        ),
        parcel_use_code_field="LUC",
        parcel_agricultural_use_codes=tuple(
            str(c) for c in range(5000, 7000, 100)
        ),
        parcel_acreage_field=None,
        parcel_owner_field="OWN1",
        parcel_owner_field_2="OWN2",
        parcel_id_field="ALTKEY",
        unincorporated_check="city_limits_layer_join",
        city_limits_layer_url=(
            "https://services1.arcgis.com/q8sarOko6mCDwiGm/arcgis/rest/"
            "services/Citrus_County_Data_WFL1/FeatureServer/9"
        ),
        city_limits_field="CORPNAME",
    ),
    "leon": CountyEndpoint(
        id="leon",
        name="Leon",
        fips=37,  # FL DOR county number for Leon (Tallahassee)
        # FLUM: FDOT District 3 D3_FLUM_County layer 5 (Leon_FLUM). Real
        # field 'FUTURELU' with distinct value 'AG' (2 features seen in
        # 500-row sample); LANDUSE description also present for eyeballs.
        # Highest-confidence FLUM ag value out of the 3 Wave-1 counties.
        flum_service_url=(
            "https://services1.arcgis.com/O1JpcwDW8sjYuddV/arcgis/rest/"
            "services/D3_FLUM_County/FeatureServer/5"
        ),
        flu_field="FUTURELU",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AG",),
        population=302713,  # BEBR 2024 estimate. Under 1.75M cap.
        confirmed_live=True,
        notes=(
            "Wave 1 (2026-07-06), refined during Wave 1 validation. "
            "PARCEL layer: PROP_USE (4-char, CAST-to-integer range "
            "check to avoid Osceola-style lexicographic false-positive "
            "trap; real sample values '5400', '5007', '6900'), "
            "CALC_ACREA (Double, acreage), OWNER1/OWNER2 (single + "
            "co-owner), TAXID (parcel ID), SALEDTE_S1/SALEDTE_S2 "
            "(sale dates -- format DECODED as MMYYYY 6-char string, "
            "see sale_date_encoding='mmyyyy_string' below and its "
            "statutory_checks.py implementation). Layer is hosted by "
            "City of Tallahassee's own GIS (intervector.leoncountyfl.gov). "
            "FLUM FUTURELU field has explicit 'AG' code (LANDUSE "
            "description 'Agricultural') -- cleanest ag_flu_values of "
            "the Wave 1 additions. Unincorporated filter left at "
            "manual_only: Leon has consolidated Tallahassee-Leon "
            "government, but the parcel layer's TAXDIST field is "
            "uniformly '1' across a 2000-row sample and no dedicated "
            "Tallahassee-city-limits FeatureServer was surfaced by "
            "AGOL search -- would need a separate Tallahassee "
            "boundary layer to distinguish incorporated vs "
            "unincorporated parcels, deferred to a future refinement."
        ),
        parcel_service_url=(
            "https://intervector.leoncountyfl.gov/intervector/rest/"
            "services/MapServices/TLC_OverlayParnal_D_WM/MapServer/0"
        ),
        parcel_use_code_field="PROP_USE",
        parcel_agricultural_use_codes=tuple(
            str(c) for c in range(5000, 7000, 100)
        ) + ("5001","5002","5003","5004","5005","5006","5007","5008","5009"),
        parcel_acreage_field="CALC_ACREA",
        parcel_owner_field="OWNER1",
        parcel_owner_field_2="OWNER2",
        parcel_id_field="TAXID",
        # Sale-date encoding decoded 2026-07-06: SALEDTE_S1 is a 6-char
        # string in MMYYYY format (e.g. '042025' = Apr 2025). See
        # statutory_checks.sold_on_or_after_cutoff for the parser.
        sale_date_encoding="mmyyyy_string",
        sale_date_field="SALEDTE_S1",
    ),
    # =====================================================================
    # Wave 2b additions (2026-07-06). Parcel data sourced via SWFWMD's
    # shared parcel_search MapServer (16 counties as separate layer IDs,
    # identical 95-field schema). parcel_source="swfwmd_parcel_search"
    # triggers the 6 AM-10 PM Eastern service-window enforcement in
    # service_windows.py. CONCENTRATION RISK: every county sharing this
    # source depends on that one third-party mirror -- schema drift or
    # outage there affects all of them simultaneously.
    # FLUM sources are each county's OWN official layer (validated with
    # FLUM-at-parcel-centroid on 3 known ag parcels each, per Wave 1's
    # discipline lesson).
    # =====================================================================
    "sarasota": CountyEndpoint(
        id="sarasota",
        name="Sarasota",
        fips=58,  # FL DOR county number for Sarasota
        # FLUM: Sarasota County's OWN official server (ags3.scgov.net =
        # Sarasota County Gov). Layer 0 (FutureLandUse). Field `flucode`,
        # ag values 'RURAL' (39x in full paginated distinct) + 'SRURAL'
        # (7x) -- CONFIRMED via FLUM-at-parcel-centroid test showing
        # ag parcels sitting in MODR/MEDR/LDR being enclave candidates
        # (which is why the ag_flu_values list must NOT include MODR
        # etc, only the still-rural categories).
        flum_service_url=(
            "https://ags3.scgov.net/server/rest/services/Hosted/"
            "FutureLandUse/FeatureServer/0"
        ),
        flu_field="flucode",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("RURAL", "SRURAL"),
        population=464223,  # BEBR 2024 estimate. Under 1.75M cap.
        confirmed_live=True,
        notes=(
            "Wave 2b (2026-07-06). PARCEL layer via SWFWMD's shared "
            "parcel_search MapServer layer 15 -- identical 95-field "
            "schema shared with 15 other FL counties (Charlotte, Citrus, "
            "DeSoto, Hardee, Hernando, Highlands, Hillsborough, Lake, "
            "Levy, Manatee, Marion, Pasco, Pinellas, Polk, Sumter). "
            "Real ag parcels confirmed via live sample: BYRD LARRY 22ac "
            "PARUSECODE='062' (Pasture), all 3 known-ag test parcels "
            "correctly sit in FLUM 'MODR' (residential development "
            "designation) -- exactly the enclave candidates the tool "
            "targets. FLUM ag_flu_values = ('RURAL', 'SRURAL') "
            "confirmed via FLUM-at-parcel-centroid. AREANO is the "
            "polygon-computed acreage (Double, always populated); ACRES "
            "(deed-recorded) is often null so should NOT be used. "
            "SWFWMD service is only available 6 AM-10 PM Eastern -- "
            "window enforcement wired via parcel_source below."
        ),
        parcel_service_url=(
            "https://www25.swfwmd.state.fl.us/arcgis12/rest/services/"
            "BaseVector/parcel_search/MapServer/15"
        ),
        parcel_source="swfwmd_parcel_search",
        parcel_use_code_field="PARUSECODE",
        parcel_agricultural_use_code_range=("050", "069"),
        parcel_acreage_field="AREANO",
        parcel_owner_field="OWNNAME",
        parcel_owner_field_2=None,
        parcel_id_field="PARNO",
        # SALE1_YEAR is a SmallInteger sale-year field -- clean year_only
        # encoding, no need to decode combined date strings.
        sale_date_encoding="year_only",
        sale_year_field="SALE1_YEAR",
    ),
    "manatee": CountyEndpoint(
        id="manatee",
        name="Manatee",
        fips=41,  # FL DOR county number for Manatee
        # FLUM: Manatee County's OWN official server (mymanatee.org =
        # Manatee County Gov). opendata/Planning layer 1 ('Future Land
        # Use'). Real fields: FLUTYPE (String, includes 'AG', 'RES',
        # 'CON', etc.), FLULABEL (more granular like 'AG-R', 'RES-6').
        # FLUTYPE is null on some rows so FLULABEL is the safer choice
        # for uniform matching. ag_flu_values = ('AG-R',) CONFIRMED via
        # FLUM-at-parcel-centroid on 3 known SWFWMD ag parcels (DAKIN
        # 340ac, MANNING 279ac + 197ac) -- all 3 correctly sit in
        # FLULABEL='AG-R'.
        flum_service_url=(
            "https://www.mymanatee.org/gisits/rest/services/opendata/"
            "Planning/FeatureServer/1"
        ),
        flu_field="FLULABEL",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AG-R",),
        population=451540,  # BEBR 2024 estimate. Under 1.75M cap.
        confirmed_live=True,
        notes=(
            "Wave 2b (2026-07-06). PARCEL layer via SWFWMD's shared "
            "parcel_search MapServer layer 10. FLUM via Manatee County's "
            "own official mymanatee.org opendata Planning service layer "
            "1. ag_flu_values=('AG-R',) confirmed via FLUM-at-centroid "
            "test on 3 known ag parcels. FLUTYPE field alternatively "
            "carries 'AG' for these but is null on other rows -- "
            "FLULABEL is more reliably populated. SWFWMD 6-10 PM ET "
            "window enforcement wired via parcel_source."
        ),
        parcel_service_url=(
            "https://www25.swfwmd.state.fl.us/arcgis12/rest/services/"
            "BaseVector/parcel_search/MapServer/10"
        ),
        parcel_source="swfwmd_parcel_search",
        parcel_use_code_field="PARUSECODE",
        parcel_agricultural_use_code_range=("050", "069"),
        parcel_acreage_field="AREANO",
        parcel_owner_field="OWNNAME",
        parcel_owner_field_2=None,
        parcel_id_field="PARNO",
        sale_date_encoding="year_only",
        sale_year_field="SALE1_YEAR",
    ),
    "hardee": CountyEndpoint(
        id="hardee",
        name="Hardee",
        fips=25,
        # FLUM: Hardee County's OWN official server (gis.hardeecounty.net).
        # LandUseZoning/MapServer layer 16 'Future Landuse (County)'.
        # Fields LANDUSECODE + LANDUSEDESC. Full paginated distinct: AGR
        # (136x AGRICULTURE), RVG (9x RURAL VILLAGE), CON (12x
        # CONSERVATION), RCN (28x RURAL CENTER), plus CITY (multi-desc),
        # TCN, HMX, RMX, COM, IND, RES-L, PBI.
        # FLUM-at-parcel-centroid on 3 real ag parcels (SEMINOLE ELECTRIC
        # 575ac in IND+REC+CON = enclave candidate; MOONEY FAMILY 398ac
        # in AGR+CON+RVG = surrounded by ag; SHADOWLAWN 94ac in RVG+AGR)
        # confirmed AGR + RVG + CON as ag/undeveloped categories.
        flum_service_url=(
            "https://gis.hardeecounty.net/arcgis/rest/services/"
            "LandUseZoning/MapServer/16"
        ),
        flu_field="LANDUSECODE",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=("AGR", "RVG", "CON"),
        population=25915,  # BEBR 2024 estimate (very small county)
        confirmed_live=True,
        notes=(
            "Wave 2b (2026-07-06). PARCEL via SWFWMD layer 4. FLUM at "
            "Hardee County's own official gis.hardeecounty.net "
            "LandUseZoning/MapServer layer 16. ag_flu_values validated "
            "via FLUM-at-parcel-centroid: AGR (Agriculture, 136x -- "
            "primary), RVG (Rural Village, 9x), CON (Conservation, "
            "12x) all included as non-residential-development. TCN "
            "(Town Center), HMX (Highway Mixed Use), RMX (Residential "
            "Mixed Use), COM (Commerce Park), IND (Industrial), CITY, "
            "RES-L (Residential Low), PBI (Public Institutional) all "
            "TREATED AS QUALIFYING for encirclement. Very small county "
            "(~26k pop)."
        ),
        parcel_service_url=(
            "https://www25.swfwmd.state.fl.us/arcgis12/rest/services/"
            "BaseVector/parcel_search/MapServer/4"
        ),
        parcel_source="swfwmd_parcel_search",
        parcel_use_code_field="PARUSECODE",
        parcel_agricultural_use_code_range=("050", "069"),
        parcel_acreage_field="AREANO",
        parcel_owner_field="OWNNAME",
        parcel_owner_field_2=None,
        parcel_id_field="PARNO",
        sale_date_encoding="year_only",
        sale_year_field="SALE1_YEAR",
    ),
    "charlotte": CountyEndpoint(
        id="charlotte",
        name="Charlotte",
        fips=15,
        # FLUM: Charlotte County's OWN official CCBOCC server
        # (agis.charlottecountyfl.gov). Essentials/CCGIS_Web_Layers2022
        # /MapServer layer 42 'Future Land Use'. Field NEWLU (alias
        # 'Future Land Use'). Full distinct: 'Low Density Residential'
        # (553x), 'Commercial' (99x), 'City' (91x = incorporated),
        # 'Preservation' (46x), 'High Density Residential', 'Medium
        # Density Residential', 'Parks & Recreation' (30x), 'Agriculture'
        # (16x), 'DRI Mixed Use', 'Coastal Residential', 'Low Intensity
        # Industrial', 'Resource Conservation' (11x), 'Charlotte Harbor
        # Mixed Use', 'Rural Estate Residential' (4x), 'Rural Community
        # Mixed Use' (3x), and smaller categories.
        # FLUM-at-parcel-centroid on 3 real ag parcels: VITALE LARRY
        # (22ac DOR-055 Timberland) sits in 'Low Density Residential' =
        # enclave candidate; NAJMI PROPERTIES in
        # Preservation+LDR+Public Lands; ACORN PORT CHARLOTTE (48ac
        # DOR-055) in Commercial+Preservation.
        flum_service_url=(
            "https://agis.charlottecountyfl.gov/arcgis/rest/services/"
            "Essentials/CCGIS_Web_Layers2022/MapServer/42"
        ),
        flu_field="NEWLU",
        jurisdiction_field=None,
        acreage_field=None,
        agricultural_flu_values=(
            "Agriculture",
            "Rural Estate Residential",
            "Rural Community Mixed Use",
            "Preservation",
            "Resource Conservation",
        ),
        population=210369,  # BEBR 2024 estimate. Under 1.75M cap.
        confirmed_live=True,
        notes=(
            "Wave 2b (2026-07-06). PARCEL via SWFWMD layer 1. FLUM at "
            "Charlotte County's own official CCBOCC server "
            "(agis.charlottecountyfl.gov) CCGIS_Web_Layers2022 layer 42. "
            "Real field NEWLU. ag_flu_values covers Agriculture + rural "
            "variants + preservation/conservation categories. Test "
            "parcels confirmed enclave-candidate pattern: DOR ag parcels "
            "sitting in Low Density Residential + Commercial FLUM (the "
            "target case). Coastal Residential + Charlotte Harbor Coastal "
            "Residential deliberately NOT included as ag -- they're "
            "residential categories."
        ),
        parcel_service_url=(
            "https://www25.swfwmd.state.fl.us/arcgis12/rest/services/"
            "BaseVector/parcel_search/MapServer/1"
        ),
        parcel_source="swfwmd_parcel_search",
        parcel_use_code_field="PARUSECODE",
        parcel_agricultural_use_code_range=("050", "069"),
        parcel_acreage_field="AREANO",
        parcel_owner_field="OWNNAME",
        parcel_owner_field_2=None,
        parcel_id_field="PARNO",
        sale_date_encoding="year_only",
        sale_year_field="SALE1_YEAR",
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
